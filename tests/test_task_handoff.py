import asyncio
import json
import re
import sys
import time

import pytest

from webcodex.config import Client, ServerConfig
from webcodex.server import Service

pytest_plugins = ["test_harness"]


async def test_mcp_handoff_is_explicit_and_resumes_the_same_execution_once(harness):
    async with harness.mcp() as client:

        async def call(name, arguments, *, window="current-window"):
            result = await client.call_tool(name, arguments, meta={"openai/session": window})
            assert not result.isError, result
            return result.structuredContent

        task = (await call("task_create", {"project": "local/project", "title": "Handoff"}))["id"]
        presented = await call("task_present", {"task_id": task})
        assert presented["continuation"]["handoff"]["reason"] == "not_connected"
        view = {"task_id": task, "view_id": "1" * 32}
        bound = await call("task_app", {**view, "action": "bind"})
        view["generation"] = bound["control"]["generation"]
        job = await call(
            "exec_command",
            {
                "task_id": task,
                "argv": [sys.executable, "-c", "import time; time.sleep(3); print('once')"],
                "yield_time_ms": 0,
                "request_id": "execute-once",
            },
        )
        handoff = job["continuation"]["handoff"]
        assert handoff["state"] == "available"
        assert handoff["condition"] == "only_when_no_independent_work"
        action = handoff["next_action"]
        assert action["tool"] == "task_wait"
        assert action["arguments"]["job_ids"] == [job["job_id"]]
        assert job["next_action"]["tool"] == "task_wait"
        assert job["next_action"]["arguments"] == action["arguments"]
        assert job["next_action"]["condition"] == "only_when_no_independent_work"
        assert job["observation_action"]["tool"] == "read_process"
        observed = await call("job_get", {"job_id": job["job_id"]})
        assert observed["continuation"]["handoff"]["next_action"] == action
        restored = await call("task_get", {"task_id": task})
        assert restored["continuation"]["handoff"]["next_action"] == action
        assert restored["progress"]["next_actions"][0]["tool"] == "task_wait"
        assert restored["progress"]["observation_actions"][0]["tool"] == "read_process"
        assert "yield" in restored["progress"]["next_step"]
        assert harness.app.state.service.continuations.current(task) is None

        read = await call(
            "read_process", {"task_id": task, "process_id": job["result"]["process_id"]}
        )
        action = read["next_action"]
        assert action["tool"] == "task_wait"
        assert action["arguments"]["job_ids"] == [read["job_id"]]
        waiting = await call(action["tool"], action["arguments"])
        assert waiting["continuation"]["handoff"]["state"] == "watching"
        assert "report what is running and yield" in waiting["progress"]["next_step"]
        assert "next_action" not in waiting["continuation"]["handoff"]
        wait_id = waiting["continuation"]["wait"]["id"]
        assert (await call(action["tool"], action["arguments"]))["continuation"]["wait"][
            "id"
        ] == wait_id

        deadline = time.monotonic() + 15
        while True:
            state = await call("task_app", {**view, "action": "state"})
            if state["continuation"]["handoff"]["state"] == "ready":
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        prepared = await call("task_app", {**view, "action": "prepare", "wait_id": wait_id})
        assert prepared["continuation"]["handoff"]["state"] == "prepared"
        assert "next_action" not in prepared["continuation"]["handoff"]
        message = prepared["control"]["app_protocol"]["message"]
        resume = json.loads(re.search(r"\{[^{}]+\}", message)[0])
        resumed = await call("task_resume", resume)
        assert resumed["already_consumed"] is False
        assert resumed["task"]["continuation"]["handoff"]["state"] == "consumed"
        assert (await call("task_resume", resume))["already_consumed"] is True
        assert len(harness.runner.processes.list(task)) == 1


@pytest.fixture
def waiting_service(tmp_path):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={
                "owner": Client(token="c" * 40, projects=["local/project"], approval_required=set())
            },
            runners={"local": "r" * 40},
        )
    )
    service.store.register("local", "instance", {"project": "b" * 64})
    task = service.store.create_task("owner", "local/project", "Waiting")
    service.continuations.bind(task["id"], "1" * 32, "window")
    try:
        yield service, task
    finally:
        service.store.close()


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing_window", "missing_window"),
        ("another_window", "another_window"),
        ("paused", "paused"),
        ("read_only", "read_only"),
        ("expired", "expired"),
    ],
)
@pytest.mark.parametrize("existing_wait", [False, True])
async def test_handoff_requires_the_current_enabled_writable_view(
    waiting_service, case, reason, existing_wait
):
    service, task = waiting_service
    job = service.store.enqueue(task, "workspace_info", {}, "existing", False, 100)
    wait = (
        service.continuations.arm(task["id"], [job["job_id"]], "explicit")
        if existing_wait
        else None
    )
    window = None if case == "missing_window" else "other" if case == "another_window" else "window"
    if case == "paused":
        service.store.db.execute("UPDATE task_controllers SET paused=1")
    elif case == "read_only":
        service.config.clients["owner"].permissions = {"read"}
    elif case == "expired":
        service.store.db.execute("UPDATE task_controllers SET lease_until=0")
    result = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window=window)
    handoff = result["continuation"]["handoff"]
    expected = {
        "state": "blocked",
        "condition": "only_when_no_independent_work",
        "reason": reason,
    }
    if reason == "expired":
        expected["next_action"] = {"tool": "task_present", "arguments": {"task_id": task["id"]}}
        assert result["next_action"]["tool"] == "task_present"
        assert result["observation_action"]["tool"] == "job_get"
    assert handoff == expected
    if existing_wait:
        assert result["continuation"]["wait"] == {"id": wait["id"], "state": "watching"}
        assert service.continuations.current(task["id"])["state"] == "watching"
    else:
        assert service.continuations.current(task["id"]) is None


@pytest.mark.parametrize(("count", "reason"), [(9, "too_many_jobs"), (101, "incomplete_job_list")])
async def test_handoff_never_silently_discards_waiting_jobs(waiting_service, count, reason):
    service, task = waiting_service
    for index in range(count):
        service.store.enqueue(task, "workspace_info", {}, str(index), False, 200)
    result = await service.call("owner", "task_get", {"task_id": task["id"]}, window="window")
    handoff = result["continuation"]["handoff"]
    assert handoff["state"] == "blocked" and handoff["reason"] == reason
    assert "next_action" not in handoff
    assert service.continuations.current(task["id"]) is None


async def test_handoff_includes_running_processes_older_than_the_visible_history(waiting_service):
    service, task = waiting_service
    for index in range(101):
        job = service.store.enqueue(task, "exec_command", {}, str(index), False, 100)
        service.store.claim("local", "instance", 1)
        service.store.complete(
            "local",
            job["job_id"],
            "instance",
            "succeeded",
            {
                "process_id": f"process-{index}",
                "status": "completed" if index else "running",
                "exit_code": 0 if index else None,
            },
        )
        if not index:
            running_job = job["job_id"]
    result = await service.call("owner", "task_get", {"task_id": task["id"]}, window="window")
    assert all(p["process_id"] != "process-0" for p in result["progress"]["process_observations"])
    assert result["continuation"]["handoff"]["next_action"]["arguments"]["job_ids"] == [running_job]


@pytest.mark.parametrize("state", ["prepared", "accepted", "unknown"])
async def test_handoff_never_replaces_an_uncertain_delivery(waiting_service, state):
    service, task = waiting_service
    job = service.store.enqueue(task, "workspace_info", {}, "existing", False, 100)
    wait = service.continuations.arm(task["id"], [job["job_id"]], "explicit")
    service.store.db.execute("UPDATE task_waits SET state=? WHERE id=?", (state, wait["id"]))
    result = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    assert result["continuation"]["handoff"] == {
        "state": state,
        "condition": "only_when_no_independent_work",
    }
    assert service.continuations.current(task["id"])["id"] == wait["id"]
    assert "next_action" not in result
    assert result["observation_action"]["tool"] == "job_get"


async def test_workspace_handoff_uses_the_same_window_as_the_task(waiting_service):
    service, task = waiting_service
    result = await service.call(
        "owner", "work_on_project", {"task_id": task["id"]}, window="window"
    )
    assert result["workspace"]["continuation"]["handoff"]["state"] == "available"
    assert result["task"]["continuation"]["handoff"]["state"] == "available"
    assert result["workspace"]["next_action"]["tool"] == "task_wait"
    assert result["workspace"]["observation_action"]["tool"] == "job_get"
    assert service.continuations.current(task["id"]) is None


async def test_expired_card_has_an_executable_repair_action_before_handoff(waiting_service):
    service, task = waiting_service
    job = service.store.enqueue(task, "workspace_info", {}, "repair", False, 100)
    with service.store.db:
        service.store.db.execute("UPDATE task_controllers SET lease_until=0")
    observed = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    assert observed["next_action"] == {
        "tool": "task_present",
        "arguments": {"task_id": task["id"]},
        "condition": "only_when_no_independent_work",
    }
    assert service.continuations.current(task["id"]) is None
    service.continuations.bind(task["id"], "2" * 32, "window")
    observed = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    action = observed["next_action"]
    assert action["tool"] == "task_wait"
    waiting = await service.call("owner", action["tool"], action["arguments"], window="window")
    assert waiting["continuation"]["wait"]["state"] == "watching"
    observed = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    assert "next_action" not in observed
    assert observed["observation_action"]["arguments"]["job_id"] == job["job_id"]
    progress = (await service.call("owner", "task_get", {"task_id": task["id"]}, window="window"))[
        "progress"
    ]
    assert progress["next_actions"] == []
    assert progress["observation_actions"]


@pytest.mark.parametrize("status", ["running", "completed"])
async def test_unread_output_keeps_its_exact_cursor_before_any_handoff(waiting_service, status):
    service, task = waiting_service
    job = service.store.enqueue(task, "exec_command", {}, "output", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        job["job_id"],
        "instance",
        "succeeded",
        {"process_id": "output-process", "status": status, "has_more": True, "next_cursor": 17},
    )
    newer = service.store.enqueue(task, "exec_command", {}, "newer-completed", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        newer["job_id"],
        "instance",
        "succeeded",
        {"process_id": "newer-process", "status": "completed", "exit_code": 0},
    )
    service.store.enqueue(task, "workspace_info", {}, "other-wait", False, 100)
    result = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    assert result["next_action"]["tool"] == "read_process"
    assert result["next_action"]["arguments"]["cursor"] == 17
    assert "observation_action" not in result
    progress = (await service.call("owner", "task_get", {"task_id": task["id"]}, window="window"))[
        "progress"
    ]
    assert progress["next_actions"][0]["tool"] == "read_process"
    assert progress["next_actions"][0]["arguments"]["cursor"] == 17
    assert service.continuations.current(task["id"]) is None


@pytest.mark.parametrize("state", ["waiting_runner", "project_changed", "completed"])
async def test_output_does_not_override_task_execution_guards(waiting_service, state):
    service, task = waiting_service
    job = service.store.enqueue(task, "exec_command", {}, "guarded-output", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        job["job_id"],
        "instance",
        "succeeded",
        {"process_id": "guarded-process", "status": "running", "has_more": True, "next_cursor": 17},
    )
    with service.store.db:
        if state == "waiting_runner":
            service.store.db.execute("UPDATE runners SET seen=0")
        elif state == "project_changed":
            service.store.db.execute(
                "UPDATE tasks SET binding=? WHERE id=?", ("x" * 64, task["id"])
            )
        else:
            service.store.db.execute(
                "UPDATE tasks SET status='completed' WHERE id=?", (task["id"],)
            )
    result = await service.call("owner", "task_get", {"task_id": task["id"]}, window="window")
    assert result["progress"]["state"] == state
    assert result["progress"]["next_actions"] == []


async def test_completed_result_is_not_replaced_by_waiting_for_another_job(waiting_service):
    service, task = waiting_service
    job = service.store.enqueue(task, "exec_command", {}, "done", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        job["job_id"],
        "instance",
        "succeeded",
        {"process_id": "done-process", "status": "completed", "exit_code": 0},
    )
    service.store.enqueue(task, "workspace_info", {}, "pending", False, 100)
    result = await service.call("owner", "job_get", {"job_id": job["job_id"]}, window="window")
    assert result["continuation"]["handoff"]["state"] == "available"
    assert "next_action" not in result and result["result"]["exit_code"] == 0


@pytest.mark.parametrize(
    "wait_state", ["available", "watching", "ready", "prepared", "accepted", "unknown"]
)
@pytest.mark.parametrize("failure", ["job", "process"])
async def test_waiting_never_hides_new_execution_problems(waiting_service, wait_state, failure):
    service, task = waiting_service
    pending = service.store.enqueue(task, "workspace_info", {}, "pending", False, 100)
    service.store.claim("local", "instance", 1)
    failed = service.store.enqueue(task, "exec_command", {}, "failed", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        failed["job_id"],
        "instance",
        "failed" if failure == "job" else "succeeded",
        {"error_type": "ExecutionError"}
        if failure == "job"
        else {"process_id": "failed-process", "status": "failed", "exit_code": 1},
    )
    newer = service.store.enqueue(task, "exec_command", {}, "newer", False, 100)
    service.store.claim("local", "instance", 1)
    service.store.complete(
        "local",
        newer["job_id"],
        "instance",
        "succeeded",
        {"process_id": "newer-process", "status": "completed", "exit_code": 0},
    )
    wait = None
    if wait_state != "available":
        wait = service.continuations.arm(task["id"], [pending["job_id"]], "keep-wait")
    with service.store.db:
        service.store.db.execute("UPDATE tasks SET updated=0 WHERE id=?", (task["id"],))
        # Windows clock resolution can give consecutive process launches the same timestamp.
        service.store.db.execute(
            "UPDATE jobs SET created=1 WHERE id IN (?,?)", (failed["job_id"], newer["job_id"])
        )
        if wait:
            service.store.db.execute(
                "UPDATE task_waits SET state=? WHERE id=?", (wait_state, wait["id"])
            )
    result = await service.call("owner", "task_get", {"task_id": task["id"]}, window="window")
    progress = result["progress"]
    assert progress["state"] == "observing"
    assert "before yielding" in progress["next_step"]
    assert all(action["tool"] != "task_wait" for action in progress["next_actions"])
    assert any(
        (action["tool"] == "job_get" and action["arguments"]["job_id"] == failed["job_id"])
        if failure == "job"
        else (
            action["tool"] == "read_process"
            and action["arguments"]["process_id"] == "failed-process"
        )
        for action in progress["next_actions"]
    )
    assert (service.continuations.current(task["id"]) or {}).get("state") == (
        None if wait_state == "available" else wait_state
    )
