from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import nullcontext

import httpx
import pytest
import uvicorn

from webcodex.config import Client, RunnerConfig, ServerConfig
from webcodex.runner import Runner
from webcodex.server import create_app


@pytest.mark.parametrize("drop_ack", [False, True])
async def test_slow_result_delivery_keeps_polling_and_releases_execution_slot(
    tmp_path, monkeypatch, drop_ack
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = Runner(
        RunnerConfig(
            server_url="https://runner.test",
            runner_id="local",
            token="r" * 40,
            state_dir=tmp_path / "state",
            projects={"project": repo},
            max_concurrent_jobs=1,
        )
    )
    binding = runner.inventory()["project"]
    jobs = [
        {
            "id": f"job{index}",
            "instance": runner.instance,
            "task_id": "task",
            "project": "project",
            "binding": binding,
            "tool": "list_files",
            "arguments": {},
        }
        for index in range(2)
    ]
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    second_executed = asyncio.Event()
    executed = []
    polls_during_delivery = []
    deliveries = []
    attempted = []
    execute = runner.execute

    async def observed_execute(job):
        await execute(job)
        executed.append(job["id"])
        if len(executed) == 2:
            second_executed.set()

    monkeypatch.setattr(runner, "execute", observed_execute)

    async def handle(request):
        body = json.loads(request.content)
        if request.url.path == "/api/runner/register":
            return httpx.Response(200, json={"registered": True})
        if request.url.path == "/api/runner/poll":
            await asyncio.sleep(0.01)
            if delivery_started.is_set() and not release_delivery.is_set():
                polls_during_delivery.append(body["limit"])
            return httpx.Response(
                200, json={"jobs": [jobs.pop(0)] if body["limit"] and jobs else []}
            )
        assert request.url.path == "/api/runner/complete"
        attempted.append(body["job_id"])
        if body["job_id"] == "job0":
            delivery_started.set()
            await release_delivery.wait()
            if drop_ack and len(attempted) == 1:
                raise httpx.ReadError("Connection closed before the acknowledgement")
        deliveries.append(body["job_id"])
        return httpx.Response(200, json={"accepted": True})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "webcodex.runner.httpx.AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    running = asyncio.create_task(runner.run())
    try:
        async with asyncio.timeout(2):
            await delivery_started.wait()
            await second_executed.wait()
        assert executed == ["job0", "job1"]
        assert 1 in polls_during_delivery
        assert (
            runner.db.execute("SELECT count(*) FROM deliveries WHERE acknowledged=0").fetchone()[0]
            == 2
        )
        assert all(
            row[0] == "succeeded" for row in runner.db.execute("SELECT status FROM deliveries")
        )
        assert all(row[0] for row in runner.db.execute("SELECT result FROM deliveries"))
        release_delivery.set()
        async with asyncio.timeout(5):
            while runner.db.execute(
                "SELECT count(*) FROM deliveries WHERE acknowledged=0"
            ).fetchone()[0]:
                await asyncio.sleep(0.01)
        assert deliveries == ["job0", "job1"]
        assert attempted == (["job0", "job0", "job1"] if drop_ack else ["job0", "job1"])
        assert executed == ["job0", "job1"]
        assert all(row[0] is None for row in runner.db.execute("SELECT result FROM deliveries"))
    finally:
        release_delivery.set()
        runner.stop.set()
        await asyncio.wait_for(running, 3)


async def test_committed_result_survives_lost_ack_and_does_not_replay(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    runner = Runner(
        RunnerConfig(
            server_url=origin,
            runner_id="local",
            token="r" * 40,
            state_dir=tmp_path / "runner",
            projects={"project": repo},
        )
    )
    app = create_app(
        ServerConfig(
            state_dir=tmp_path / "server",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    service = app.state.service
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server.capture_signals = nullcontext
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    service.store.register("local", runner.instance, runner.inventory())
    task = service.store.create_task("owner", "local/project", "lost acknowledgement")
    queued = service.store.enqueue(
        task,
        "exec_command",
        {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "with Path('executions.txt').open('a', encoding='utf-8') as stream:\n"
                "    stream.write('once\\n')\n"
                "print('original-command-result')\n",
            ],
            "yield_time_ms": 2000,
        },
        "command",
        10,
    )
    claimed = service.claim("local", runner.instance, 1)[0]
    transport = httpx.AsyncHTTPTransport()
    attempts = 0

    async def lose_first_ack(request):
        nonlocal attempts
        response = await transport.handle_async_request(request)
        if request.url.path == "/api/runner/complete":
            attempts += 1
            if attempts == 1:
                assert response.status_code == 200
                await response.aread()
                await response.aclose()
                raise httpx.ReadError("Response lost after the Server committed the result")
        return response

    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("Isolated Server did not start")
                await asyncio.sleep(0.01)
        await runner.execute(claimed)
        original = runner.db.execute(
            "SELECT result FROM deliveries WHERE id=?", (queued["job_id"],)
        ).fetchone()[0]
        payload = json.loads(original)
        assert payload["exit_code"] == 0 and payload["output"].strip() == "original-command-result"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lose_first_ack),
            base_url=runner.config.server_url,
            headers={"Authorization": "Bearer " + runner.config.token},
        ) as client:
            with pytest.raises(httpx.ReadError, match="after the Server committed"):
                await runner.flush(client)
            pending = runner.db.execute(
                "SELECT acknowledged,result FROM deliveries WHERE id=?", (queued["job_id"],)
            ).fetchone()
            assert tuple(pending) == (0, original)
            assert service.store.job(queued["job_id"], "owner")["result"] == payload
            assert await runner.flush(client) == 1
            accepted = runner.db.execute(
                "SELECT acknowledged,result FROM deliveries WHERE id=?", (queued["job_id"],)
            ).fetchone()
            assert tuple(accepted) == (1, None)
            await runner.register(client)
            await runner.execute(claimed)
            assert await runner.flush(client) == 0
        assert attempts == 2
        assert (repo / "executions.txt").read_text(encoding="utf-8") == "once\n"
        assert runner.processes.db.execute("SELECT count(*) FROM processes").fetchone()[0] == 1
        assert service.store.job(queued["job_id"], "owner")["result"] == payload
    finally:
        await runner.processes.close()
        await asyncio.gather(*(extension.close() for extension in runner.extensions.values()))
        runner.db.close()
        runner.lock_file.close()
        await transport.aclose()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
