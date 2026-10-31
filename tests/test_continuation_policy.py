import asyncio
import json
import re
import sys
import time

import pytest

from webcodex.continuation import Continuations
from webcodex.store import Store

pytest_plugins = ["test_harness"]


@pytest.fixture
def task_state(tmp_path):
    store = Store(tmp_path / "state")
    store.register("local", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "local/project", "Keep working")
    continuation = Continuations(store)
    generation = continuation.bind(task["id"], "1" * 32, "window")["generation"]
    yield continuation, task["id"], generation
    store.close()


def ready(continuation, task, request):
    wait = continuation.arm(task, [], request)
    continuation.refresh(wait["id"], wait["jobs"], [], True)
    with continuation.db:
        continuation.db.execute(
            "UPDATE task_waits SET ready_at=? WHERE id=?", (time.time() - 3, wait["id"])
        )
    return wait["id"]


def resume_args(prepared):
    return json.loads(re.search(r"\{[^{}]+\}", prepared["app_protocol"]["message"])[0])


def test_default_off_manual_double_click_and_audited_unknown(task_state):
    continuation, task, generation = task_state
    wait = ready(continuation, task, "one")
    args = (task, "1" * 32, generation, "window")
    assert not continuation.public(task, "window")["auto_enabled"]
    with pytest.raises(ValueError, match="disabled"):
        continuation.action(*args, "prepare", wait)
    assert continuation.public(task)["deliveries"] == []
    first = continuation.action(*args, "prepare_manual", wait, request_id="click")
    second = continuation.action(*args, "prepare_manual", wait, request_id="click")
    assert second["duplicate"] and "app_protocol" not in second
    token = resume_args(first)["consume_token"]
    continuation.action(*args, "finish", wait, "unknown")
    history = continuation.public(task)["deliveries"]
    assert len(history) == 1 and history[0]["status"] == "unknown"
    assert history[0]["reason"] == "user_resume" and history[0]["sent_at"]
    assert token not in history[0]["content"] and "[REDACTED]" in history[0]["content"]
    assert continuation.policy(task)["auto_message_count"] == 0
    assert not continuation.consume(task, wait, token)["already_consumed"]
    assert continuation.consume(task, wait, token)["already_consumed"]


def test_automatic_limit_interval_and_manual_recovery(task_state):
    continuation, task, generation = task_state
    args = (task, "1" * 32, generation, "window")
    continuation.action(*args, "configure", max_auto_messages=2, min_interval_seconds=60)
    continuation.action(*args, "enable")
    one = ready(continuation, task, "one")
    prepared = continuation.action(*args, "prepare", one)
    continuation.consume(task, one, resume_args(prepared)["consume_token"])
    two = ready(continuation, task, "two")
    with pytest.raises(ValueError, match="interval"):
        continuation.action(*args, "prepare", two)
    with continuation.db:
        continuation.db.execute(
            "UPDATE task_deliveries SET created=created-61,sent_at=sent_at-61 WHERE task_id=?",
            (task,),
        )
    prepared = continuation.action(*args, "prepare", two)
    continuation.consume(task, two, resume_args(prepared)["consume_token"])
    three = ready(continuation, task, "three")
    with pytest.raises(ValueError, match="limit"):
        continuation.action(*args, "prepare", three)
    assert continuation.current(task)["state"] == "ready"
    assert continuation.policy(task)["auto_message_count"] == 2
    manual = continuation.action(*args, "prepare_manual", three)
    assert manual["app_protocol"]["message"]
    assert continuation.policy(task)["auto_message_count"] == 2


def test_unsent_reservation_restores_quota_and_fences_late_finish(task_state):
    continuation, task, generation = task_state
    args = (task, "1" * 32, generation, "window")
    continuation.action(*args, "configure", max_auto_messages=1, min_interval_seconds=60)
    continuation.action(*args, "enable")
    first_wait = ready(continuation, task, "one")
    first = continuation.action(*args, "prepare", first_wait, request_id="attempt-one")
    original_token = resume_args(first)["consume_token"]
    continuation.action(*args, "finish", first_wait, "not_sent")
    policy = continuation.policy(task)
    assert policy["auto_message_count"] == 0
    assert policy["last_auto_at"] is None and policy["next_auto_at"] is None
    assert continuation.public(task)["deliveries"][0]["status"] == "not_sent"
    assert continuation.public(task)["deliveries"][0]["sent_at"] is None
    replacement = continuation.current(task)
    assert replacement["id"] != first_wait
    continuation.refresh(replacement["id"], replacement["jobs"], [], True)
    with continuation.db:
        continuation.db.execute(
            "UPDATE task_waits SET ready_at=? WHERE id=?", (time.time() - 3, replacement["id"])
        )
    second = continuation.action(*args, "prepare", replacement["id"], request_id="attempt-two")
    with pytest.raises(ValueError, match="no longer current"):
        continuation.action(*args, "finish", first_wait, "not_sent")
    with pytest.raises(ValueError, match="invalid"):
        continuation.consume(task, first_wait, original_token)
    assert continuation.current(task)["state"] == "prepared"
    continuation.action(*args, "finish", replacement["id"], "unknown")
    with pytest.raises(ValueError, match="Only a prepared"):
        continuation.action(*args, "finish", replacement["id"], "not_sent")
    assert continuation.policy(task)["auto_message_count"] == 1
    continuation.consume(task, replacement["id"], resume_args(second)["consume_token"])


def test_automatic_interval_starts_after_delivery_not_preparation(task_state, monkeypatch):
    continuation, task, generation = task_state
    args = (task, "1" * 32, generation, "window")
    now = time.time()
    monkeypatch.setattr("webcodex.continuation.time.time", lambda: now)
    continuation.action(*args, "enable")
    first_wait = ready(continuation, task, "one")
    first = continuation.action(*args, "prepare", first_wait)
    now += 50
    continuation.consume(task, first_wait, resume_args(first)["consume_token"])
    second_wait = ready(continuation, task, "two")
    now += 20
    with pytest.raises(ValueError, match="interval"):
        continuation.action(*args, "prepare", second_wait)
    now += 41
    assert continuation.action(*args, "prepare", second_wait)["state"] == "prepared"


def test_bind_reconnect_restart_and_takeover_never_restore_automatic_consent(task_state):
    continuation, task, generation = task_state
    args = (task, "1" * 32, generation, "window")
    continuation.action(*args, "enable")
    assert continuation.public(task)["auto_enabled"]
    continuation.bind(task, "1" * 32, "window")
    assert not continuation.public(task)["auto_enabled"]
    continuation.action(*args, "enable")
    Continuations(continuation.store)
    assert not continuation.public(task)["auto_enabled"]
    continuation.action(*args, "enable")
    wait = ready(continuation, task, "ready")
    original = continuation.action(*args, "prepare", wait)
    token = resume_args(original)["consume_token"]
    replacement = continuation.takeover(task, "2" * 32, "new-window")
    assert replacement["generation"] > generation
    assert not continuation.public(task)["auto_enabled"]
    assert continuation.public(task, "window")["controller"]["state"] == "other"
    with pytest.raises(ValueError, match="no longer"):
        continuation.action(*args, "finish", wait, "accepted")
    with pytest.raises(ValueError, match="invalid"):
        continuation.consume(task, wait, token)
    assert continuation.bind(task, "1" * 32, "window")["bound"] is False
    assert continuation.current(task)["state"] == "ready"
    assert continuation.policy(task)["auto_message_count"] == 1


def test_expired_controller_is_reported_and_can_be_reclaimed(task_state):
    continuation, task, generation = task_state
    continuation.action(task, "1" * 32, generation, "window", "enable")
    with continuation.db:
        continuation.db.execute(
            "UPDATE task_controllers SET lease_until=0 WHERE task_id=?", (task,)
        )
    observed = continuation.public(task, "new-window")
    assert observed["controller"]["state"] == "expired"
    assert observed["controller"]["updated_at"] > 0
    assert not observed["auto_enabled"]
    assert continuation.bind(task, "2" * 32, "new-window")["bound"]
    assert not continuation.public(task)["auto_enabled"]


async def test_long_command_finishes_in_same_turn_without_any_delivery(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Same turn"}
        )
    )["id"]
    view = {"task_id": task, "view_id": "1" * 32}
    bound = await service.call("owner", "task_app", {**view, "action": "bind"}, window="window")
    view["generation"] = bound["control"]["generation"]
    job = await service.call(
        "owner",
        "exec_command",
        {
            "task_id": task,
            "argv": [
                sys.executable,
                "-c",
                "import time; time.sleep(.4); print('same-turn-result')",
            ],
            "yield_time_ms": 0,
            "request_id": "run-once",
        },
        window="window",
    )
    async with asyncio.timeout(15):
        while job.get("next_action"):
            action = job["next_action"]
            assert action["tool"] in {"job_get", "read_process"}
            job = await service.call("owner", action["tool"], action["arguments"], window="window")
    assert job["result"]["exit_code"] == 0
    assert "same-turn-result" in job["result"]["output"]
    assert len(harness.runner.processes.list(task)) == 1
    for _ in range(3):
        snapshot = await service.call(
            "owner", "task_app", {**view, "action": "state"}, window="window"
        )
        assert snapshot["continuation"]["deliveries"] == []
        assert snapshot["progress"]["liveness"]["host_turn_state"] == "unknown"


async def test_new_manual_click_after_consumption_has_its_own_delivery(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Manual continuation"}
        )
    )["id"]
    view = {"task_id": task, "view_id": "1" * 32}
    bound = await service.call("owner", "task_app", {**view, "action": "bind"}, window="window")
    view["generation"] = bound["control"]["generation"]
    first = await service.call(
        "owner",
        "task_app",
        {**view, "action": "prepare_manual", "request_id": "first-click"},
        window="window",
    )
    await service.call("owner", "task_resume", resume_args(first["control"]), window="window")
    retry = await service.call(
        "owner",
        "task_app",
        {**view, "action": "prepare_manual", "request_id": "first-click"},
        window="window",
    )
    assert retry["control"]["duplicate"] is True
    second = await service.call(
        "owner",
        "task_app",
        {**view, "action": "prepare_manual", "request_id": "second-click"},
        window="window",
    )
    assert second["control"]["state"] == "prepared"
    assert second["control"]["wait_id"] != first["control"]["wait_id"]
    assert len(second["continuation"]["deliveries"]) == 2
    assert second["continuation"]["policy"]["auto_message_count"] == 0


async def test_card_monitoring_refreshes_completed_process_with_messages_disabled(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Monitor only"}
        )
    )["id"]
    view = {"task_id": task, "view_id": "1" * 32}
    bound = await service.call("owner", "task_app", {**view, "action": "bind"}, window="window")
    view["generation"] = bound["control"]["generation"]
    job = await service.call(
        "owner",
        "exec_command",
        {
            "task_id": task,
            "argv": [sys.executable, "-c", "import time; time.sleep(.3); print('monitor-only')"],
            "yield_time_ms": 0,
            "request_id": "original-command",
        },
        window="window",
    )
    while job["status"] in {"queued", "running"}:
        job = await service.call("owner", "job_get", {"job_id": job["job_id"], "wait_seconds": 1})
    assert job["result"]["status"] == "running"
    await asyncio.sleep(0.4)
    with service.store.db:
        service.store.db.execute(
            "UPDATE jobs SET updated=? WHERE id=?", (time.time() - 11, job["job_id"])
        )
    state = await service.call("owner", "task_app", {**view, "action": "state"}, window="window")
    assert state["progress"]["liveness"]["work"]["running_count"] == 0
    assert state["continuation"]["auto_enabled"] is False
    assert state["continuation"]["deliveries"] == []
    assert state["continuation"]["wait"] is None
    assert len(harness.runner.processes.list(task)) == 1
    model = await service.call("owner", "task_get", {"task_id": task}, window="window")
    action = model["progress"]["output_actions"][0]
    assert action["tool"] == "read_process" and action["arguments"]["cursor"] == 0
    observed = await service.call("owner", action["tool"], action["arguments"], window="window")
    assert "monitor-only" in observed["result"]["output"]
    enabled = await service.call("owner", "task_app", {**view, "action": "enable"}, window="window")
    assert enabled["continuation"]["wait"] is None
    assert enabled["continuation"]["deliveries"] == []


async def test_opt_in_batches_existing_jobs_and_never_rearms_from_observations(harness):
    service = harness.app.state.service
    task = (
        await service.call("owner", "task_create", {"project": "local/project", "title": "Batch"})
    )["id"]
    view = {"task_id": task, "view_id": "1" * 32}
    bound = await service.call("owner", "task_app", {**view, "action": "bind"}, window="window")
    view["generation"] = bound["control"]["generation"]
    await service.call("owner", "task_app", {**view, "action": "enable"}, window="window")
    jobs = []
    for n in range(2):
        jobs.append(
            await service.call(
                "owner",
                "exec_command",
                {
                    "task_id": task,
                    "argv": [sys.executable, "-c", f"import time; time.sleep(.2); print({n})"],
                    "yield_time_ms": 0,
                    "request_id": f"run-{n}",
                },
                window="window",
            )
        )
    async with asyncio.timeout(15):
        while True:
            state = await service.call(
                "owner", "task_app", {**view, "action": "state"}, window="window"
            )
            if state["continuation"]["wait"] and state["continuation"]["wait"]["state"] == "ready":
                break
            await asyncio.sleep(0.05)
    wait = state["continuation"]["wait"]["id"]
    members = service.store.rows("SELECT job_id FROM task_watch_members WHERE wait_id=?", (wait,))
    assert {r["job_id"] for r in members} == {j["job_id"] for j in jobs}
    with service.store.db:
        service.store.db.execute(
            "UPDATE task_waits SET ready_at=? WHERE id=?", (time.time() - 3, wait)
        )
    prepared = await service.call(
        "owner", "task_app", {**view, "action": "prepare", "wait_id": wait}, window="window"
    )
    resume = resume_args(prepared["control"])
    await service.call("owner", "task_resume", resume, window="window")
    for _ in range(2):
        state = await service.call(
            "owner", "task_app", {**view, "action": "state"}, window="window"
        )
        assert state["continuation"]["wait"]["state"] == "consumed"
        assert state["continuation"]["policy"]["auto_message_count"] == 1
    assert len(harness.runner.processes.list(task)) == 2


async def test_takeover_returns_handoff_and_enforces_task_owner(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Implement feature"}
        )
    )["id"]
    await service.call(
        "owner",
        "task_update",
        {
            "task_id": task,
            "status": "active",
            "summary": "Parser fixed",
            "plan": [{"step": "Verify browser", "status": "pending"}],
            "handoff": {
                "changed_files": ["src/parser.py"],
                "validation": ["12 parser tests passed"],
                "issues": [],
                "next_steps": ["Verify browser"],
            },
        },
    )
    service.continuations.bind(task, "1" * 32, "old")
    with pytest.raises(ValueError, match="Task not found"):
        await service.call("reviewer", "task_takeover", {"task_id": task}, window="new")
    result = await service.call("owner", "task_takeover", {"task_id": task}, window="new")
    assert result["handoff"]["goal"] == "Implement feature"
    assert result["handoff"]["changed_files"] == ["src/parser.py"]
    assert result["handoff"]["validation"] == ["12 parser tests passed"]
    assert result["handoff"]["next_steps"] == ["Verify browser"]
    assert not result["continuation"]["auto_enabled"]
    assert service.continuations.bind(task, "1" * 32, "old")["bound"] is False
