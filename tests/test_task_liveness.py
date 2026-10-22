import asyncio
import json
import sqlite3
import time

import pytest

from webcodex.config import Client, ServerConfig
from webcodex.continuation import fingerprint
from webcodex.server import Service
from webcodex.store import new_id

pytest_plugins = ["test_harness"]


@pytest.fixture
def activity_service(tmp_path):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={
                owner: Client(token=token * 40, projects=["local/project"], approval_required=set())
                for owner, token in [("owner", "c"), ("other", "d")]
            },
            runners={"local": "r" * 40},
        )
    )
    service.store.register("local", "instance", {"project": "b" * 64})
    task = service.store.create_task("owner", "local/project", "Activity")
    try:
        yield service, task
    finally:
        service.store.close()
        service.diagnostics.close()


async def test_mcp_and_http_activity_exclude_card_polling_and_payloads(harness):
    service = harness.app.state.service
    secret = "private-prompt-consume_token-and-request-payload"
    async with harness.mcp() as client:
        created = await client.call_tool(
            "task_create",
            {"project": "local/project", "title": secret},
            meta={"openai/session": "chat"},
        )
        assert not created.isError
        task_id = created.structuredContent["id"]
        window = fingerprint("window\0chat")
        previous = service.store.tool_activity(task_id, window)
        assert previous["tool"] == "task_create" and previous["outcome"] == "returned"
        assert previous["finished_at"] >= previous["started_at"]
        task = service.store.task(task_id, "owner")
        running = service.store.enqueue(task, "exec_command", {}, "observed-process", False, 100)
        instance = service.store.one("SELECT instance FROM runners WHERE id='local'")["instance"]
        service.store.claim("local", instance, 1)
        service.store.complete(
            "local",
            running["job_id"],
            instance,
            "succeeded",
            {"process_id": "observed-process", "status": "running"},
        )
        observed_at = 1700000000.0
        with service.store.db:
            service.store.db.execute(
                "UPDATE jobs SET updated=? WHERE id=?", (observed_at, running["job_id"])
            )
        view = {"task_id": task_id, "view_id": "1" * 32}
        bound = await client.call_tool(
            "task_app", {**view, "action": "bind"}, meta={"openai/session": "chat"}
        )
        view["generation"] = bound.structuredContent["control"]["generation"]
        assert bound.structuredContent["progress"]["liveness"]["conversation"] == previous
        assert bound.structuredContent["progress"]["liveness"]["work"] == {
            "running_count": 1,
            "pending_count": 0,
            "observed_at": observed_at,
        }
        review = await client.call_tool("task_review", {"task_id": task_id})
        assert not review.isError
        state = await client.call_tool(
            "task_app", {**view, "action": "state"}, meta={"openai/session": "chat"}
        )
        liveness = state.structuredContent["progress"]["liveness"]
        assert liveness["conversation"] == previous
        assert (
            liveness["observed_at"]
            >= bound.structuredContent["progress"]["liveness"]["observed_at"]
        )
        assert liveness["runner_last_seen"] > 0
        assert secret not in json.dumps(liveness) and "trace_id" not in json.dumps(liveness)
    async with harness.http() as client:
        response = await client.post(
            "/api/call", json={"name": "task_get", "arguments": {"task_id": task_id}}
        )
        assert response.status_code == 200
        assert "liveness" in response.json()["progress"]
    assert service.store.tool_activity(task_id, window) == previous
    async with harness.mcp() as client:
        other = await client.call_tool(
            "task_get", {"task_id": task_id}, meta={"openai/session": "other-chat"}
        )
        assert not other.isError
        state = await client.call_tool(
            "task_app", {**view, "action": "state"}, meta={"openai/session": "chat"}
        )
        assert state.structuredContent["progress"]["liveness"]["conversation"] is None
    assert service.store.one("SELECT count(*) AS count FROM task_tool_activity")["count"] == 1


async def test_mcp_request_is_visible_while_running_then_finishes(harness, monkeypatch):
    service = harness.app.state.service
    task = service.store.create_task("owner", "local/project", "Running")
    original = service.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(owner, name, arguments, *, window=None):
        if name == "task_get":
            entered.set()
            await release.wait()
        return await original(owner, name, arguments, window=window)

    monkeypatch.setattr(service, "call", blocked)
    async with harness.mcp() as client:
        request = asyncio.create_task(client.call_tool("task_get", {"task_id": task["id"]}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            running = service.task_presentation(task)["progress"]["liveness"]["conversation"]
            assert running["tool"] == "task_get" and running["outcome"] == "running"
            assert running["finished_at"] is None
            release.set()
            assert not (await request).isError
            finished = service.task_presentation(task)["progress"]["liveness"]["conversation"]
            assert finished["outcome"] == "returned"
            assert finished["started_at"] == running["started_at"]
            assert finished["finished_at"] >= running["started_at"]
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["returned", "error", "cancelled"])
async def test_request_outcomes_remain_observable(activity_service, monkeypatch, outcome):
    service, task = activity_service

    async def complete(owner, name, arguments, *, window=None):
        if outcome == "error":
            raise ValueError("private payload")
        if outcome == "cancelled":
            raise asyncio.CancelledError("private payload")
        return {"status": "succeeded"}

    monkeypatch.setattr(service, "call", complete)
    request = service.traced_call("owner", "task_get", {"task_id": task["id"]}, track_activity=True)
    if outcome == "error":
        with pytest.raises(ValueError, match="private payload"):
            await request
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await request
    else:
        await request
    observed = service.task_presentation(task)["progress"]["liveness"]["conversation"]
    assert observed["outcome"] == outcome and observed["finished_at"] >= observed["started_at"]
    assert set(observed) == {"tool", "started_at", "finished_at", "outcome"}
    assert "private payload" not in json.dumps(observed)


async def test_returned_tool_error_is_distinct_from_a_failed_process(activity_service, monkeypatch):
    service, task = activity_service

    async def complete(owner, name, arguments, *, window=None):
        return {"status": "succeeded", "result": {"status": "failed", "exit_code": 1}}

    monkeypatch.setattr(service, "call", complete)
    await service.traced_call("owner", "task_get", {"task_id": task["id"]}, track_activity=True)
    assert service.store.tool_activity(task["id"])["outcome"] == "returned"

    async def rejected(owner, name, arguments, *, window=None):
        return {"status": "failed", "result": {"error": "private error"}}

    monkeypatch.setattr(service, "call", rejected)
    await service.traced_call("owner", "task_get", {"task_id": task["id"]}, track_activity=True)
    assert service.store.tool_activity(task["id"])["outcome"] == "error"


@pytest.mark.parametrize("tool", ["task_get", "job_get", "job_cancel"])
async def test_foreign_owner_and_revoked_project_cannot_change_activity(activity_service, tool):
    service, task = activity_service
    job = service.store.enqueue(task, "workspace_info", {}, "pending", False, 100)
    await service.traced_call("owner", "task_events", {"task_id": task["id"]}, track_activity=True)
    previous = service.store.tool_activity(task["id"])
    arguments = {"task_id": task["id"]} if tool == "task_get" else {"job_id": job["job_id"]}
    with pytest.raises(ValueError, match="not found"):
        await service.traced_call("other", tool, arguments, track_activity=True)
    assert service.store.tool_activity(task["id"]) == previous
    service.config.clients["owner"].projects = []
    with pytest.raises(ValueError, match="not authorized"):
        await service.traced_call("owner", tool, arguments, track_activity=True)
    assert service.store.tool_activity(task["id"]) == previous


@pytest.mark.parametrize("tool", ["job_get", "job_cancel"])
async def test_job_only_requests_are_associated_with_the_owned_task(activity_service, tool):
    service, task = activity_service
    job = service.store.enqueue(task, "workspace_info", {}, "pending", False, 100)
    await service.traced_call("owner", tool, {"job_id": job["job_id"]}, track_activity=True)
    observed = service.store.tool_activity(task["id"])
    assert observed["tool"] == tool and observed["finished_at"] is not None
    assert service.store.one("SELECT count(*) AS count FROM task_tool_activity")["count"] == 1


@pytest.mark.parametrize("older_first", [False, True])
async def test_older_parallel_completion_cannot_replace_newer_activity(
    activity_service, monkeypatch, older_first
):
    service, task = activity_service
    original = service.call
    entered = {tool: asyncio.Event() for tool in ["task_get", "task_events"]}
    release = {tool: asyncio.Event() for tool in entered}
    monkeypatch.setattr(time, "time", lambda: 1700000000.0)

    async def blocked(owner, name, arguments, *, window=None):
        entered[name].set()
        await release[name].wait()
        return await original(owner, name, arguments, window=window)

    monkeypatch.setattr(service, "call", blocked)
    context = {"trace_id": "1" * 32}
    older = asyncio.create_task(
        service.traced_call(
            "owner",
            "task_get",
            {"task_id": task["id"]},
            diagnostic_context=context,
            track_activity=True,
        )
    )
    newer = None
    try:
        await asyncio.wait_for(entered["task_get"].wait(), 5)
        newer = asyncio.create_task(
            service.traced_call(
                "owner",
                "task_events",
                {"task_id": task["id"]},
                diagnostic_context=context,
                track_activity=True,
            )
        )
        await asyncio.wait_for(entered["task_events"].wait(), 5)
        latest = service.store.tool_activity(task["id"])
        assert latest["tool"] == "task_events" and latest["started_at"] == 1700000000.0
        if older_first:
            release["task_get"].set()
            await older
            assert service.store.tool_activity(task["id"]) == latest
        release["task_events"].set()
        await newer
        finished = service.store.tool_activity(task["id"])
        release["task_get"].set()
        await older
        assert service.store.tool_activity(task["id"]) == finished
        assert finished["tool"] == "task_events" and finished["outcome"] == "returned"
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(older, return_exceptions=True)
        if newer:
            await asyncio.gather(newer, return_exceptions=True)


@pytest.mark.parametrize("interleaved", [False, True])
@pytest.mark.parametrize("frozen", [False, True])
async def test_new_workspace_result_is_linked_without_overwriting_a_newer_request(
    activity_service, monkeypatch, interleaved, frozen
):
    service, _ = activity_service
    original = service.call
    entered, release = asyncio.Event(), asyncio.Event()
    if frozen:
        monkeypatch.setattr(time, "time", lambda: 1700000000.0)

    async def workspace(owner, name, arguments, *, window=None):
        if name == "workspace_info":
            entered.set()
            await release.wait()
            return {"task_id": arguments["task_id"], "status": "succeeded", "result": {}}
        return await original(owner, name, arguments, window=window)

    monkeypatch.setattr(service, "call", workspace)
    request = asyncio.create_task(
        service.traced_call(
            "owner",
            "work_on_project",
            {"project": "local/project", "title": "Workspace", "request_id": "workspace"},
            track_activity=True,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task = service.store.one("SELECT * FROM tasks WHERE request_id='workspace'")
        if interleaved:
            await service.traced_call(
                "owner", "task_events", {"task_id": task["id"]}, track_activity=True
            )
        release.set()
        assert (await request)["task"]["id"] == task["id"]
        observed = service.store.tool_activity(task["id"])
        assert observed["tool"] == ("task_events" if interleaved else "work_on_project")
        assert observed["outcome"] == "returned"
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


async def test_server_reconstruction_marks_only_running_requests_interrupted(activity_service):
    service, task = activity_service
    await service.traced_call("owner", "task_events", {"task_id": task["id"]}, track_activity=True)
    finished = service.store.tool_activity(task["id"])
    unfinished = service.store.create_task("owner", "local/project", "Unfinished")
    service.store.start_tool_activity(unfinished["id"], new_id(), "read_process", time.time())
    config = service.config
    service.store.close()
    service.diagnostics.close()
    restored = Service(config)
    try:
        assert restored.store.tool_activity(task["id"]) == finished
        interrupted = restored.task_presentation(unfinished)["progress"]["liveness"]["conversation"]
        assert interrupted["outcome"] == "interrupted"
        assert interrupted["finished_at"] >= interrupted["started_at"]
        assert restored.store.one("SELECT count(*) AS count FROM task_tool_activity")["count"] == 2
    finally:
        restored.store.close()
        restored.diagnostics.close()


def test_liveness_exposes_runner_observation_without_a_conversation(activity_service):
    service, task = activity_service
    first = service.task_presentation(task)["progress"]["liveness"]
    assert first["conversation"] is None and first["runner_last_seen"] > 0
    assert first["work"] == {"running_count": 0, "pending_count": 0, "observed_at": None}
    service.store.enqueue(task, "workspace_info", {}, "pending", False, 100)
    assert service.task_presentation(task)["progress"]["liveness"]["work"]["pending_count"] == 1
    with service.store.db:
        service.store.db.execute("DELETE FROM runners")
    latest = service.task_presentation(task)["progress"]["liveness"]
    assert latest["conversation"] is None and latest["runner_last_seen"] is None
    assert latest["observed_at"] >= first["observed_at"]


def test_work_counts_include_observations_outside_the_bounded_detail_lists(activity_service):
    service, task = activity_service
    store = service.store
    for tool in ["exec_command", "subagent_spawn"]:
        job = store.enqueue(task, tool, {}, tool, False, 200)
        store.claim("local", "instance", 1)
        result = {"process_id": job["job_id"], "status": "running"}
        if tool == "subagent_spawn":
            result["agent_id"] = job["job_id"]
        store.complete("local", job["job_id"], "instance", "succeeded", result)
        with store.db:
            store.db.execute("UPDATE jobs SET updated=10 WHERE id=?", (job["job_id"],))
            store.db.execute(
                "UPDATE agent_observations SET updated=20 WHERE job_id=?", (job["job_id"],)
            )
    for index in range(100):
        job = store.enqueue(task, "exec_command", {}, str(index), False, 200)
        store.claim("local", "instance", 1)
        store.complete(
            "local",
            job["job_id"],
            "instance",
            "succeeded",
            {
                "process_id": f"completed-{index}",
                "status": "completed",
                "exit_code": 0,
            },
        )
    for index in range(101):
        store.enqueue(task, "workspace_info", {}, f"pending-{index}", False, 200)
    progress = service.task_progress(task)
    assert len(progress["pending_jobs"]) == len(progress["process_observations"]) == 100
    assert all(process["status"] == "completed" for process in progress["process_observations"])
    assert service.task_presentation(task)["progress"]["liveness"]["work"] == {
        "running_count": 2,
        "pending_count": 101,
        "observed_at": 10.0,
    }


@pytest.mark.parametrize("phase", ["start", "finish"])
@pytest.mark.parametrize("failure", [None, ValueError, sqlite3.OperationalError])
async def test_activity_write_failure_preserves_the_original_tool_result_or_exception(
    activity_service, monkeypatch, phase, failure
):
    service, task = activity_service
    events = []
    monkeypatch.setattr(
        service.diagnostics, "record", lambda event, **fields: events.append((event, fields))
    )
    original_error = failure("original private error") if failure else None
    original_result = {"status": "succeeded", "result": {"value": "original result"}}

    async def original(owner, name, arguments, *, window=None):
        if original_error:
            raise original_error
        return original_result

    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("private telemetry error")

    monkeypatch.setattr(service, "call", original)
    monkeypatch.setattr(service.store, phase + "_tool_activity", broken)
    request = service.traced_call("owner", "task_get", {"task_id": task["id"]}, track_activity=True)
    if original_error:
        with pytest.raises(failure) as caught:
            await request
        assert caught.value is original_error
    else:
        assert await request is original_result
    activity_errors = [fields for event, fields in events if event == "activity.error"]
    assert activity_errors == [{"exception_type": "DatabaseError"}]
    assert "private telemetry error" not in json.dumps(events)


def test_activity_read_failure_returns_unknown_without_losing_task_progress(
    activity_service, monkeypatch
):
    service, task = activity_service
    original = service.store.one

    def read(sql, params=()):
        if "FROM task_tool_activity" in sql:
            raise sqlite3.OperationalError("private activity read error")
        return original(sql, params)

    monkeypatch.setattr(service.store, "one", read)
    progress = service.task_presentation(task)["progress"]
    assert progress["liveness"]["conversation"] is None
    assert progress["runner_online"]


async def test_non_mcp_traced_calls_do_not_create_conversation_activity(activity_service):
    service, task = activity_service
    await service.traced_call("owner", "task_get", {"task_id": task["id"]})
    assert service.store.tool_activity(task["id"]) is None
