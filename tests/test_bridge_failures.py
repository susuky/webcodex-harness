import asyncio
import json
import socket
import sys
import time
from contextlib import asynccontextmanager, nullcontext
from datetime import timedelta

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from test_harness import SOURCE_ROOT

pytest_plugins = ["test_harness"]


@asynccontextmanager
async def interrupted_proxy(harness, mode):
    dropped = asyncio.Event()
    progress_sent = []
    async with harness.http() as upstream:

        async def proxy(request):
            body = await request.body()
            message = json.loads(body) if body else {}
            forwarded = {
                name: request.headers[name]
                for name in ("accept", "content-type", "mcp-protocol-version")
                if name in request.headers
            }
            response = await upstream.request(
                request.method, "/mcp", content=body, headers=forwarded
            )
            if (
                message.get("method") == "tools/call"
                and message.get("params", {}).get("name") == "exec_command"
                and not dropped.is_set()
                and response.status_code == 200
            ):
                token = message["params"].get("_meta", {}).get("progressToken")
                dropped.set()

                async def incomplete():
                    yield b": ping\n\n"
                    if mode == "progress":
                        for index in range(60):
                            progress_sent.append(index)
                            notification = {
                                "jsonrpc": "2.0",
                                "method": "notifications/progress",
                                "params": {"progressToken": token, "progress": index},
                            }
                            yield (
                                "event: message\ndata: " + json.dumps(notification) + "\n\n"
                            ).encode()
                            await asyncio.sleep(0.1)

                return StreamingResponse(incomplete(), media_type="text/event-stream")
            return Response(
                response.content,
                status_code=response.status_code,
                headers={"content-type": response.headers.get("content-type", "application/json")},
            )

        app = Starlette(routes=[Route("/mcp", proxy, methods=["POST", "GET", "DELETE"])])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
        )
        server.capture_signals = nullcontext
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}", dropped, progress_sent
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            listener.close()


@pytest.mark.parametrize("mode", ["eof", "progress"])
async def test_incomplete_sse_times_out_one_rpc_without_replay_or_poisoning_the_bridge(
    harness, tmp_path, mode
):
    async with interrupted_proxy(harness, mode) as (url, dropped, progress_sent):
        bootstrap = (
            "import asyncio,sys; from datetime import timedelta; import webcodex.bridge as bridge; "
            "bridge.RESPONSE_TIMEOUT=timedelta(seconds=2); "
            "asyncio.run(bridge.run_bridge(sys.argv[1],sys.argv[2]))"
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, url, "c" * 40],
            env={"PYTHONPATH": SOURCE_ROOT},
        )
        progress_received = []

        async def progress(value, total=None, message=None):
            progress_received.append(value)

        with (tmp_path / "bridge.log").open("w") as log:
            async with (
                stdio_client(parameters, errlog=log) as streams,
                ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=5)
                ) as client,
            ):
                await client.initialize()
                task = await client.call_tool(
                    "task_create", {"project": "local/project", "title": "Interrupted stream"}
                )
                task_id = task.structuredContent["id"]
                started = time.monotonic()
                interrupted = asyncio.create_task(
                    client.call_tool(
                        "exec_command",
                        {
                            "task_id": task_id,
                            "request_id": "execute-once",
                            "yield_time_ms": 0,
                            "argv": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path; p=Path('once.txt'); "
                                "p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('done')",
                            ],
                        },
                        progress_callback=progress,
                    )
                )
                try:
                    await asyncio.wait_for(dropped.wait(), 3)
                    assert not interrupted.done()
                    parallel = await client.call_tool("task_events", {"task_id": task_id})
                    assert not parallel.isError
                    failed = await interrupted
                    assert failed.isError
                    assert "Timed out while waiting for response" in failed.content[0].text
                    assert time.monotonic() - started < 4
                    if mode == "progress":
                        assert len(progress_sent) > 2
                        assert len(progress_received) > 2
                    jobs = harness.app.state.service.store.rows(
                        "SELECT id FROM jobs WHERE task_id=? AND tool='exec_command'", (task_id,)
                    )
                    assert len(jobs) == 1
                    recovered = await client.call_tool("job_get", {"job_id": jobs[0]["id"]})
                    assert not recovered.isError
                    process_id = recovered.structuredContent["result"]["process_id"]
                    observed = await client.call_tool(
                        "read_process", {"task_id": task_id, "process_id": process_id}
                    )
                    assert not observed.isError
                    assert observed.structuredContent["result"]["status"] == "completed"
                    assert (harness.repo / "once.txt").read_text() == "x"
                    assert len(harness.runner.processes.list(task_id)) == 1
                finally:
                    interrupted.cancel()
                    await asyncio.gather(interrupted, return_exceptions=True)
