from __future__ import annotations

import asyncio
import socket
import sys
from contextlib import nullcontext

import uvicorn
from test_harness import call, process_result

from webcodex.server import create_app

pytest_plugins = ["test_harness"]


async def test_server_reload_preserves_runner_and_existing_background_command(harness):
    async with harness.mcp() as client:
        task = await call(
            client, "task_create", {"project": "local/project", "title": "Reload continuity"}
        )
        script = (
            "import os,time;from pathlib import Path;"
            "Path('command.pid').write_text(str(os.getpid()));"
            "print('started',flush=True);\n"
            "while not Path('release-command').exists():\n"
            " with Path('command.ticks').open('ab') as f:f.write(b'.')\n"
            " time.sleep(.05)\n"
            "print('finished',flush=True)\n"
        )
        original = await call(
            client,
            "exec_command",
            {
                "task_id": task["id"],
                "argv": [sys.executable, "-u", "-c", script],
                "yield_time_ms": 100,
                "timeout_seconds": 45,
                "request_id": "one-command-across-reload",
            },
        )
    pid = int((harness.repo / "command.pid").read_text())
    ticks_before = (harness.repo / "command.ticks").stat().st_size
    instance = harness.runner.instance
    harness.server.should_exit = True
    await asyncio.wait_for(harness.server_task, 10)
    assert not harness.runner_task.done()
    assert (harness.repo / "command.ticks").stat().st_size > ticks_before

    harness.socket = socket.socket()
    harness.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    harness.socket.bind(("127.0.0.1", harness.port))
    harness.app = create_app(harness.config)
    harness.server = uvicorn.Server(
        uvicorn.Config(harness.app, log_level="error", access_log=False)
    )
    harness.server.capture_signals = nullcontext
    harness.server_task = asyncio.create_task(harness.server.serve(sockets=[harness.socket]))
    async with asyncio.timeout(10):
        while not harness.server.started:
            await asyncio.sleep(0.02)
    assert harness.runner.instance == instance
    assert int((harness.repo / "command.pid").read_text()) == pid
    (harness.repo / "release-command").touch()
    async with harness.mcp() as client:
        result = await process_result(client, task["id"], original)
        assert result["exit_code"] == 0
        assert "finished" in result["output"]
        saved = await call(client, "job_get", {"job_id": original["job_id"]})
        assert saved["result"]["process_id"] == original["result"]["process_id"]
        connections = await call(client, "connection_status")
        assert connections["sources"][0]["status"] == "available"
