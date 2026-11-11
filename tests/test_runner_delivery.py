from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from webcodex.config import RunnerConfig
from webcodex.runner import Runner


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
        release_delivery.set()
        async with asyncio.timeout(5):
            while runner.db.execute(
                "SELECT count(*) FROM deliveries WHERE acknowledged=0"
            ).fetchone()[0]:
                await asyncio.sleep(0.01)
        assert deliveries == ["job0", "job1"]
        assert attempted == (["job0", "job0", "job1"] if drop_ack else ["job0", "job1"])
        assert executed == ["job0", "job1"]
    finally:
        release_delivery.set()
        runner.stop.set()
        await asyncio.wait_for(running, 3)
