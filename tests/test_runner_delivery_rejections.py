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


@pytest.fixture
async def delivery_state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    app = create_app(
        ServerConfig(
            state_dir=tmp_path / "server",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
            max_pending_jobs=1,
        )
    )
    runner = Runner(
        RunnerConfig(
            server_url=origin,
            runner_id="local",
            token="r" * 40,
            state_dir=tmp_path / "runner",
            projects={"project": project},
        )
    )
    service = app.state.service
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server.capture_signals = nullcontext
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("Isolated Server did not start")
                await asyncio.sleep(0.01)
        service.store.register("local", runner.instance, runner.inventory())
        task = service.store.create_task("owner", "local/project", "rejected delivery")
        yield app, service, runner, task["id"]
    finally:
        await runner.processes.close()
        await asyncio.gather(*(extension.close() for extension in runner.extensions.values()))
        runner.db.close()
        runner.lock_file.close()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()


@pytest.mark.parametrize("rejected_count", [1, 101])
async def test_rejected_old_deliveries_do_not_block_real_command_results(
    delivery_state, rejected_count
):
    _, service, runner, task_id = delivery_state
    submitted = await service.call(
        "owner",
        "exec_command",
        {
            "task_id": task_id,
            "argv": [sys.executable, "-c", "print('executed once')"],
            "request_id": "new-command",
            "yield_time_ms": 2000,
        },
    )
    job_id = submitted["job_id"]
    claimed = service.claim("local", runner.instance, 1)
    assert len(claimed) == 1
    await runner.execute(claimed[0])
    saved_error = json.dumps({"error": "Historical job is absent from the Server"})
    # UUID4 job IDs sort after these IDs, including across the 100-row delivery page.
    rejected_ids = [f"{index:032x}" for index in range(rejected_count)]
    with runner.db:
        runner.db.executemany(
            "INSERT INTO deliveries(id,instance,status,result,acknowledged) "
            "VALUES (?,?,'failed',?,0)",
            [(identifier, runner.instance, saved_error) for identifier in rejected_ids],
        )
    result = json.loads(
        runner.db.execute("SELECT result FROM deliveries WHERE id=?", (job_id,)).fetchone()[0]
    )
    assert result["output"].strip() == "executed once" and result["exit_code"] == 0

    async with httpx.AsyncClient(
        base_url=runner.config.server_url,
        headers={"Authorization": "Bearer " + runner.config.token},
    ) as client:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await runner.flush(client)
        assert failure.value.response.status_code == 400
        accepted = service.store.job(job_id, "owner")
        assert accepted["status"] == "succeeded"
        assert accepted["result"]["output"].strip() == "executed once"
        acknowledged = runner.db.execute(
            "SELECT acknowledged,result FROM deliveries WHERE id=?", (job_id,)
        ).fetchone()
        assert tuple(acknowledged) == (1, None)
        await runner.execute(claimed[0])
        assert (
            runner.processes.db.execute(
                "SELECT count(*) FROM processes WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            == 1
        )
        rejected = list(
            runner.db.execute(
                "SELECT id,status,result,acknowledged FROM deliveries WHERE acknowledged=0 ORDER BY id"
            )
        )
        assert [row["id"] for row in rejected] == rejected_ids
        assert all(
            (row["status"], row["result"], row["acknowledged"]) == ("failed", saved_error, 0)
            for row in rejected
        )
        # A delivered result frees the owner's existing queue slot for an explicit new command.
        next_job = await service.call(
            "owner",
            "exec_command",
            {"task_id": task_id, "argv": [sys.executable, "-c", "print('next')"]},
        )
        assert next_job["status"] == "queued"


@pytest.mark.parametrize("failure", [400, 503, "transport"])
async def test_delivery_failures_preserve_retry_backoff(delivery_state, failure):
    _, _, runner, _ = delivery_state
    with runner.db:
        runner.db.executemany(
            "INSERT INTO deliveries(id,instance,status,result) VALUES (?,?,'succeeded','{}')",
            [("a-old", runner.instance), ("b-new", runner.instance)],
        )
    attempted = []
    first_attempt = asyncio.Event()
    next_attempt = asyncio.Event()

    async def handle(request):
        job_id = json.loads(request.content)["job_id"]
        attempted.append(job_id)
        if job_id == "a-old":
            first_attempt.set()
            if failure == "transport":
                raise httpx.ReadError("Temporary response loss")
            return httpx.Response(failure, json={"error": "Rejected"})
        next_attempt.set()
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url=runner.config.server_url
    ) as client:
        delivering = asyncio.create_task(runner.deliver_pending(client))
        try:
            await asyncio.wait_for(first_attempt.wait(), 1)
            if failure == 400:
                await asyncio.wait_for(next_attempt.wait(), 1)
            await asyncio.sleep(0.05)
            assert attempted == (["a-old", "b-new"] if failure == 400 else ["a-old"])
            assert (
                runner.db.execute(
                    "SELECT acknowledged FROM deliveries WHERE id='a-old'"
                ).fetchone()[0]
                == 0
            )
        finally:
            runner.stop.set()
            runner.delivery_ready.set()
            await asyncio.wait_for(delivering, 1)
