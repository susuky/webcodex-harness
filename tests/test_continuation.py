import asyncio
import json
import re
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_harness import SOURCE_ROOT

from webcodex.continuation import APP_MIME, APP_URI, Continuations

pytest_plugins = ["test_harness"]


async def test_stdio_task_app_waits_for_same_execution_and_consumes_once(harness, tmp_path):
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"server_url": harness.url, "token": "c" * 40}))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "webcodex", "bridge", "--config", str(config)],
        env={"PYTHONPATH": SOURCE_ROOT},
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as client:
        initialized = await client.initialize()
        assert initialized.capabilities.resources is not None
        specs = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert specs["task_present"].meta["ui"]["resourceUri"] == APP_URI
        assert specs["task_app"].meta["ui"]["visibility"] == ["app"]
        resource = (await client.list_resources()).resources[0]
        assert str(resource.uri) == APP_URI and resource.mimeType == APP_MIME
        html = (await client.read_resource(resource.uri)).contents[0]
        assert html.mimeType == APP_MIME and "ui/message" in html.text

        async def call(name, arguments, *, window="first-window", error=False):
            result = await client.call_tool(name, arguments, meta={"openai/session": window})
            assert result.isError is error, result
            return result.structuredContent if not error else result.content[0].text

        task = (await call("task_create", {"project": "local/project", "title": "Continuation"}))[
            "id"
        ]
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
        args = {"task_id": task, "job_ids": [job["job_id"]], "request_id": "wait-once"}
        assert "No active task App" in await call(
            "task_wait", args, window="other-window", error=True
        )
        waiting = await call("task_wait", args)
        wait_id = waiting["continuation"]["wait"]["id"]
        assert (await call("task_wait", args))["continuation"]["wait"]["id"] == wait_id
        deadline = time.monotonic() + 15
        while True:
            state = await call("task_app", {**view, "action": "state"})
            if state["continuation"]["wait"]["state"] == "ready":
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        prepared = await call("task_app", {**view, "action": "prepare", "wait_id": wait_id})
        message = prepared["control"]["app_protocol"]["message"]
        resume = json.loads(re.search(r"\{[^{}]+\}", message)[0])
        assert resume["task_id"] == task and resume["wait_id"] == wait_id
        assert "not available" in await call(
            "task_app", {**view, "action": "prepare", "wait_id": wait_id}, error=True
        )
        # The model may consume before the Host acknowledgement returns.
        assert (await call("task_resume", resume))["already_consumed"] is False
        assert (await call("task_resume", resume))["already_consumed"] is True
        finished = await call(
            "task_app", {**view, "action": "finish", "wait_id": wait_id, "outcome": "accepted"}
        )
        assert finished["control"]["state"] == "consumed"
        processes = harness.runner.processes.list(task)
        assert len(processes) == 1 and processes[0]["status"] == "completed"
        stored = harness.app.state.service.continuations.current(task)
        assert resume["consume_token"] not in json.dumps(stored)
        assert "first-window" not in json.dumps(
            harness.app.state.service.store.rows("SELECT * FROM task_controllers")
        )


async def test_task_view_replacement_restart_and_uncertain_delivery(harness):
    service = harness.app.state.service
    task = (
        await service.call("owner", "task_create", {"project": "local/project", "title": "Fences"})
    )["id"]
    store = service.continuations
    assert store.bind(task, "0" * 32, None) == {"bound": False, "reason": "missing_window"}
    assert not store.connected(task, None)
    first = store.bind(task, "1" * 32, "window-one")
    assert store.bind(task, "2" * 32, "window-two")["bound"] is False
    second = store.bind(task, "2" * 32, "window-one")
    assert second["generation"] > first["generation"]
    assert store.bind(task, "1" * 32, "window-one") == {"bound": False, "reason": "replaced_view"}
    # A pending wait remains durable through service reconstruction.
    wait = store.arm(task, [], "durable-wait")
    store.refresh(wait["id"], wait["jobs"], [], True)
    prepared = store.action(
        task, "2" * 32, second["generation"], "window-one", "prepare", wait["id"]
    )
    assert "app_protocol" in prepared
    restored = Continuations(service.store)
    assert restored.current(task)["state"] == "prepared"
    with service.store.db:
        service.store.db.execute(
            "UPDATE task_controllers SET lease_until=0 WHERE task_id=?", (task,)
        )
    third = restored.bind(task, "3" * 32, "window-one")
    assert third["generation"] > second["generation"]
    assert restored.current(task)["state"] == "unknown"
    # Repeated long-close recovery does not depend on a stale endpoint stored in the card.
    with service.store.db:
        service.store.db.execute(
            "UPDATE task_controllers SET lease_until=0 WHERE task_id=?", (task,)
        )
    assert restored.bind(task, "4" * 32, "window-one")["generation"] > third["generation"]
    assert restored.current(task)["state"] == "unknown"
    async with harness.mcp("d" * 40) as reviewer:
        denied = await reviewer.call_tool(
            "task_app", {"task_id": task, "view_id": "5" * 32, "action": "bind"}
        )
        assert denied.isError and "Task not found" in denied.content[0].text


async def test_manual_recovery_pause_revocation_and_completion(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Recovery"}
        )
    )["id"]
    store = service.continuations
    view = "1" * 32
    generation = store.bind(task, view, "window")["generation"]
    wait = store.arm(task, [], "first")
    store.refresh(wait["id"], wait["jobs"], [], True)
    store.action(task, view, generation, "window", "pause")
    with pytest.raises(ValueError, match="not available"):
        store.action(task, view, generation, "window", "prepare", wait["id"])
    store.action(task, view, generation, "window", "enable")
    prepared = store.action(task, view, generation, "window", "prepare", wait["id"])
    resume = json.loads(re.search(r"\{[^{}]+\}", prepared["app_protocol"]["message"])[0])
    with pytest.raises(ValueError, match="invalid"):
        store.consume(task, wait["id"], "incorrect-token" * 4)
    store.action(task, view, generation, "window", "pause")
    with pytest.raises(ValueError, match="paused"):
        store.consume(task, wait["id"], resume["consume_token"])
    store.action(task, view, generation, "window", "enable")
    store.action(task, view, generation, "window", "finish", wait["id"], "unknown")
    with pytest.raises(ValueError, match="unconsumed"):
        store.arm(task, [], "replacement")
    replacement = store.arm(task, [], "replacement", wait["id"])
    assert replacement["id"] != wait["id"]
    assert store.arm(task, [], "replacement", wait["id"])["id"] == replacement["id"]
    with pytest.raises(ValueError, match="invalid"):
        store.consume(task, wait["id"], resume["consume_token"])
    service.config.clients["owner"].permissions.discard("write")
    with pytest.raises(ValueError, match="not permitted"):
        await service.call(
            "owner",
            "task_app",
            {
                "task_id": task,
                "action": "prepare",
                "view_id": view,
                "generation": generation,
                "wait_id": replacement["id"],
            },
            window="window",
        )
    service.config.clients["owner"].permissions.add("write")
    await service.call(
        "owner",
        "task_update",
        {
            "task_id": task,
            "status": "completed",
            "summary": "Checked",
            "plan": [],
        },
    )
    assert store.current(task)["state"] == "cancelled"
    with pytest.raises(ValueError, match="already completed"):
        await service.call("owner", "task_resume", resume)


async def test_failed_execution_becomes_ready_without_rerun(harness):
    service = harness.app.state.service
    task = (
        await service.call(
            "owner", "task_create", {"project": "local/project", "title": "Failed command"}
        )
    )["id"]
    service.continuations.bind(task, "1" * 32, "window")
    job = await service.call(
        "owner",
        "exec_command",
        {
            "task_id": task,
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            "yield_time_ms": 1000,
            "request_id": "failed-command",
        },
    )
    await service.call(
        "owner",
        "task_wait",
        {
            "task_id": task,
            "job_ids": [job["job_id"]],
            "request_id": "failed-wait",
        },
        window="window",
    )
    deadline = time.monotonic() + 10
    while service.continuations.current(task)["state"] != "ready":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
        await service.refresh_task_wait(service.store.task(task, "owner"))
    process = harness.runner.processes.list(task)
    assert len(process) == 1 and process[0]["exit_code"] == 7
