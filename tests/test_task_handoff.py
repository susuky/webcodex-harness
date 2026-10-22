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
        assert job["next_action"]["tool"] == "read_process"
        observed = await call("job_get", {"job_id": job["job_id"]})
        assert observed["continuation"]["handoff"]["next_action"] == action
        restored = await call("task_get", {"task_id": task})
        assert restored["continuation"]["handoff"]["next_action"] == action
        assert "then yield" in restored["progress"]["next_step"]
        assert harness.app.state.service.continuations.current(task) is None

        read = await call(
            "read_process", {"task_id": task, "process_id": job["result"]["process_id"]}
        )
        action = read["continuation"]["handoff"]["next_action"]
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
    assert handoff == {
        "state": "blocked",
        "condition": "only_when_no_independent_work",
        "reason": reason,
    }
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
