from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from webcodex.config import ServerConfig
from webcodex.diagnostics import hash_id
from webcodex.server import RequestDiagnostics, StreamableHTTPSessionManager, create_app

pytest_plugins = ["test_harness"]
SOURCE_ROOT = str(Path(__file__).resolve().parents[1] / "src")


def records(harness):
    path = harness.config.state_dir / "diagnostics/server.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_bridge_and_server_share_trace_without_recording_payloads(harness, tmp_path):
    secret = "private_prompt_and_command_7d4730"
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"server_url": harness.url, "token": "c" * 40}))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "webcodex", "bridge", "--config", str(config)],
        env={"PYTHONPATH": SOURCE_ROOT},
    )
    stderr = tmp_path / "bridge.log"
    with stderr.open("w") as log:
        async with (
            stdio_client(parameters, errlog=log) as streams,
            ClientSession(*streams) as client,
        ):
            await client.initialize()
            await client.list_tools()
            response = await client.call_tool(
                "task_create",
                {"project": "local/project", "title": secret, "request_id": secret},
                meta={"openai/session": secret},
            )
            task_id = response.structuredContent["id"]
            rejected = await client.call_tool("task_get", {"task_id": secret})
            assert rejected.isError
    bridge = [json.loads(line) for line in stderr.read_text().splitlines() if line.startswith("{")]
    server = records(harness)
    created = next(
        item
        for item in server
        if item["event"] == "tool.completed" and item["tool"] == "task_create"
    )
    assert created["task_id"] == task_id and created["is_error"] is False
    assert created["window_hash"] != secret
    assert created["duration_ms"] >= 0
    assert any(
        item["event"] == "tool.completed" and item["trace_id"] == created["bridge_trace_id"]
        for item in bridge
    )
    assert created["trace_id"] == created["bridge_trace_id"]
    assert any(
        item.get("http_id") == created["http_id"] and item["event"] == "http.started"
        for item in server
    )
    assert any(
        item["event"] == "tool.error" and item.get("exception_type") == "ValueError"
        for item in server
    )
    assert all(
        secret not in json.dumps(item) and "c" * 40 not in json.dumps(item)
        for item in server + bridge
    )
    assert bridge[0]["event"] == "bridge.started"
    assert any(item["event"] == "bridge.completed" for item in bridge)


async def test_client_cancellation_keeps_a_single_trace_without_replaying_work(
    harness, monkeypatch
):
    service = harness.app.state.service
    original = service.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(owner, name, arguments, *, window=None):
        if name == "job_get":
            entered.set()
            await release.wait()
            return {"status": "succeeded", "result": {"status": "completed", "exit_code": 0}}
        return await original(owner, name, arguments, window=window)

    monkeypatch.setattr(service, "call", blocked)
    async with harness.mcp() as client:
        request = asyncio.create_task(
            client.call_tool("job_get", {"job_id": "1" * 32, "wait_seconds": 20})
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            # A stateless MCP transport can finish server work after the caller leaves.
            release.set()
            async with asyncio.timeout(5):
                while not any(
                    item["event"] in {"tool.completed", "tool.cancelled"}
                    and item.get("tool") == "job_get"
                    for item in records(harness)
                ):
                    await asyncio.sleep(0.02)
            starts = [
                item
                for item in records(harness)
                if item["event"] == "tool.started" and item.get("tool") == "job_get"
            ]
            assert len(starts) == 1
            assert any(
                item.get("trace_id") == starts[0]["trace_id"]
                and item["event"] in {"tool.completed", "tool.cancelled"}
                for item in records(harness)
            )
            assert service.store.rows("SELECT id FROM jobs") == []
        finally:
            release.set()
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_http_rejections_and_bad_arguments_keep_only_metadata(harness):
    secret = "private_unknown_tool_976dd1"
    async with harness.http(token=secret) as client:
        assert (await client.get("/api/tools")).status_code == 401
    async with harness.http() as client:
        assert (await client.post("/api/call", json={"name": secret})).status_code == 400
    items = records(harness)
    assert any(item.get("http_status") == 401 for item in items)
    assert any(item.get("http_status") == 400 for item in items)
    assert any(item.get("tool") == "unknown" and item["event"] == "tool.error" for item in items)
    assert secret not in json.dumps(items)


@pytest.mark.parametrize("termination", ["completed", "disconnected", "cancelled", "error"])
async def test_http_observer_preserves_receive_send_and_failure(termination):
    events, received, sent = [], [], []
    diagnostics = SimpleNamespace(
        record=lambda event, **fields: events.append({"event": event, **fields})
    )
    scope = {"type": "http", "method": "POST", "path": "/unknown/private-path"}
    messages = iter(
        [{"type": "http.request", "body": b"private-body"}, {"type": "http.disconnect"}]
    )

    async def receive():
        message = next(messages)
        received.append(message)
        return message

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        assert (await receive())["body"] == b"private-body"
        if termination == "disconnected":
            assert (await receive())["type"] == "http.disconnect"
        elif termination == "cancelled":
            raise asyncio.CancelledError
        elif termination == "error":
            raise OSError("private-error")
        else:
            await send({"type": "http.response.start", "status": 201})
            await send({"type": "http.response.body", "body": b"private-result"})

    observed = RequestDiagnostics(app, diagnostics)
    if termination == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await observed(scope, receive, send)
    elif termination == "error":
        with pytest.raises(OSError, match="private-error"):
            await observed(scope, receive, send)
    else:
        await observed(scope, receive, send)
    assert events[0]["event"] == "http.started"
    assert events[-1]["event"] == "http." + termination
    assert events[-1]["http_id"] == scope["state"]["http_id"]
    assert events[-1]["duration_ms"] >= 0
    assert len(received) == (2 if termination == "disconnected" else 1)
    assert len(sent) == (2 if termination == "completed" else 0)
    assert "private" not in json.dumps(events)


async def test_finish_delivery_diagnostics_are_logged_without_notification_token(harness):
    service = harness.app.state.service
    task = service.store.create_task("owner", "local/project", "delivery diagnostics")
    window = "a" * 64
    view_id = "b" * 32
    control = service.continuations.bind(task["id"], view_id, window)
    wait = service.continuations.arm(task["id"], ["c" * 32], "diagnostic-wait")
    service.continuations.refresh(wait["id"], wait["jobs"], [], True)
    prepared = service.continuations.action(
        task["id"], view_id, control["generation"], window, "prepare", wait["id"]
    )
    result = await service.traced_call(
        "owner",
        "task_app",
        {
            "task_id": task["id"],
            "view_id": view_id,
            "generation": control["generation"],
            "action": "finish",
            "wait_id": wait["id"],
            "outcome": "unknown",
            "delivery_error": "rpc_error",
            "delivery_error_code": -32603,
        },
        window=window,
        diagnostic_context={"rpc_id_hash": hash_id("private request ID")},
    )
    assert result["continuation"]["wait"]["state"] == "unknown"
    item = next(
        item
        for item in records(harness)
        if item.get("action") == "finish" and item["event"] == "tool.completed"
    )
    assert item["delivery_error"] == "rpc_error" and item["delivery_error_code"] == -32603
    assert item["wait_id"] == wait["id"] and item["wait_state"] == "unknown"
    assert prepared["app_protocol"]["message"] not in json.dumps(records(harness))
    assert "private request ID" not in json.dumps(records(harness))


@pytest.mark.parametrize("failure", ["manager", "store"])
async def test_server_always_closes_diagnostic_file_on_shutdown_failure(
    tmp_path, monkeypatch, failure
):
    @asynccontextmanager
    async def run(manager):
        yield
        if failure == "manager":
            raise RuntimeError("manager shutdown")

    monkeypatch.setattr(StreamableHTTPSessionManager, "run", run)
    app = create_app(ServerConfig(state_dir=tmp_path, admin_token="a" * 40, clients={}, runners={}))
    service = app.state.service
    close_store = service.store.close

    def close():
        close_store()
        if failure == "store":
            raise RuntimeError("store shutdown")

    monkeypatch.setattr(service.store, "close", close)
    with pytest.raises(RuntimeError, match=failure + " shutdown"):
        async with app.router.lifespan_context(app):
            service.diagnostics.record("http.started", http_id="1" * 32)
    assert service.diagnostics.logger.handlers == []
