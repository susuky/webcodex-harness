from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from webcodex.management import Management

pytest_plugins = ["test_harness", "test_task_liveness"]


async def task(client, title="管理頁驗收"):
    response = await client.post(
        "/api/call",
        json={"name": "task_create", "arguments": {"project": "local/project", "title": title}},
    )
    assert response.status_code == 200
    return response.json()["id"]


async def test_management_public_shell_authenticated_data_and_owner_boundary(harness):
    async with harness.http() as client:
        task_id = await task(client)
        response = await client.get("/manage", headers={"Authorization": ""})
        assert response.status_code == 200
        assert task_id not in response.text and "c" * 40 not in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert (
            await client.get("/api/manage/state", headers={"Authorization": ""})
        ).status_code == 401
        state = (await client.get("/api/manage/state")).json()
        assert [r["id"] for r in state["tasks"]] == [task_id]
        assert state["runners"][0]["online"]
    async with harness.http("d" * 40) as reviewer:
        assert (await reviewer.get("/api/manage/state")).json()["tasks"] == []
        assert (await reviewer.get("/api/manage/tasks/" + task_id)).status_code == 400
        assert (
            await reviewer.post("/api/manage/refresh", json={"task_id": task_id})
        ).status_code == 400


async def test_management_refresh_observes_completion_without_restarting_command(harness):
    async with harness.http() as client:
        task_id = await task(client)
        response = await client.post(
            "/api/call",
            json={
                "name": "exec_command",
                "arguments": {
                    "task_id": task_id,
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(0.2); print('finished')",
                    ],
                    "yield_time_ms": 0,
                },
            },
        )
        initial = response.json()
        deadline = time.monotonic() + 5
        while initial.get("status") in {"queued", "running"} and time.monotonic() < deadline:
            initial = (
                await client.post(
                    "/api/call",
                    json={
                        "name": "job_get",
                        "arguments": {"job_id": initial["job_id"], "wait_seconds": 1},
                    },
                )
            ).json()
        assert initial["result"]["status"] == "running"
        before = (await client.get("/api/manage/tasks/" + task_id)).json()
        activity = before["liveness"]["conversation"]
        assert activity["tool"] == "exec_command" and activity["finished_at"] > 0
        await asyncio.sleep(0.3)
        refreshed = await client.post("/api/manage/refresh", json={"task_id": task_id})
        assert refreshed.status_code == 200
        assert len(refreshed.json()["updates"]) == 1
        detail = (await client.get("/api/manage/tasks/" + task_id)).json()
        assert detail["processes"][0]["status"] == "completed"
        assert "finished" in detail["outputs"][0]["output"]
        assert detail["liveness"]["conversation"] == activity
        await client.post("/api/manage/refresh", json={"task_id": task_id})
        assert (
            harness.app.state.service.store.db.execute(
                "SELECT count(*) FROM jobs WHERE task_id=? AND tool='exec_command'", (task_id,)
            ).fetchone()[0]
            == 1
        )


async def test_management_offline_records_are_marked_and_not_reexecuted(harness):
    async with harness.http() as client:
        task_id = await task(client)
        # Freeze the registered heartbeat after stopping only this test's poll loop.
        harness.runner.stop.set()
        await harness.runner_task
        with harness.app.state.service.store.db:
            harness.app.state.service.store.db.execute(
                "UPDATE runners SET seen=?", (time.time() - 30,)
            )
        detail = (await client.get("/api/manage/tasks/" + task_id)).json()
        assert detail["stale"] is True and detail["runner_online"] is False
        assert (await client.post("/api/manage/refresh", json={"task_id": task_id})).json()[
            "updates"
        ] == []


async def test_management_respects_read_permission(harness):
    harness.app.state.service.config.clients["reviewer"].permissions = {"execute"}
    async with harness.http("d" * 40) as client:
        assert (await client.get("/api/manage/state")).status_code == 400


def test_management_keeps_old_running_processes_visible(activity_service):
    service, current_task = activity_service
    store = service.store
    first = None
    for index in range(102):
        job = store.enqueue(current_task, "exec_command", {}, str(index), 200)
        store.claim("local", "instance", 1)
        result = {"process_id": job["job_id"], "status": "running" if index == 0 else "completed"}
        if index:
            result["exit_code"] = 0
        else:
            first = job["job_id"]
        store.complete("local", job["job_id"], "instance", "succeeded", result)
    store.start_tool_activity(
        current_task["id"], "test-trace", "read_file", time.time(), window="test-conversation"
    )
    page = Management(service).describe(current_task)
    assert page["processes"][0]["process_id"] == first
    assert page["processes"][0]["status"] == "running"
    assert page["liveness"]["conversation"]["tool"] == "read_file"


async def test_management_refreshes_selected_task_beyond_first_page(harness):
    async with harness.http() as client:
        task_id = await task(client, "Old running task")
        response = await client.post(
            "/api/call",
            json={
                "name": "exec_command",
                "arguments": {
                    "task_id": task_id,
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.2); print('old task finished')",
                    ],
                    "yield_time_ms": 0,
                },
            },
        )
        initial = response.json()
        deadline = time.monotonic() + 5
        while initial.get("status") in {"queued", "running"} and time.monotonic() < deadline:
            initial = (
                await client.post(
                    "/api/call",
                    json={
                        "name": "job_get",
                        "arguments": {"job_id": initial["job_id"], "wait_seconds": 1},
                    },
                )
            ).json()
        assert initial["result"]["status"] == "running"
        store = harness.app.state.service.store
        for index in range(60):
            newer = store.create_task("owner", "local/project", "Newer task " + str(index))
            store.update_task(newer["id"], "owner", "completed", "", [])
        first = (await client.get("/api/manage/state")).json()
        assert len(first["tasks"]) == 60 and first["next_offset"] == 60
        assert task_id not in {item["id"] for item in first["tasks"]}
        second = (await client.get("/api/manage/state?offset=60")).json()
        assert [item["id"] for item in second["tasks"]] == [task_id]
        await asyncio.sleep(0.3)
        refreshed = await client.post("/api/manage/refresh", json={"task_ids": [task_id]})
        assert refreshed.status_code == 200 and len(refreshed.json()["updates"]) == 1
        detail = (await client.get("/api/manage/tasks/" + task_id)).json()
        assert detail["processes"][0]["status"] == "completed"
        assert "old task finished" in detail["outputs"][0]["output"]
        assert (
            store.one(
                "SELECT count(*) AS count FROM jobs WHERE task_id=? AND tool='exec_command'",
                (task_id,),
            )["count"]
            == 1
        )


@pytest.mark.parametrize(
    "body",
    [
        ["not-an-object"],
        {"task_ids": ["x"] * 101},
        {"task_ids": "x"},
        {"task_ids": [None]},
        {"task_id": "x", "task_ids": []},
    ],
)
async def test_management_refresh_rejects_invalid_selection(harness, body):
    async with harness.http() as client:
        assert (await client.post("/api/manage/refresh", json=body)).status_code == 400


def test_management_activity_excludes_internal_observations_and_tracks_latest_call(
    activity_service,
    monkeypatch,
):
    service, current_task = activity_service
    store = service.store
    # Windows Python 3.11/3.12 wall clocks can return the same timestamp for
    # several calls. Keep event ordering explicit instead of relying on clock resolution.
    clock = time.time()

    def next_timestamp():
        nonlocal clock
        clock += 0.05
        return clock

    monkeypatch.setattr("webcodex.store.time", SimpleNamespace(time=next_timestamp))

    def complete(tool, request):
        job = store.enqueue(current_task, tool, {}, request, 200)
        store.claim("local", "instance", 1)
        store.complete("local", job["job_id"], "instance", "succeeded", {})
        return job["job_id"]

    first = complete("read_file", "real-call")
    management = Management(service)
    initial = management.describe(current_task)["liveness"]["conversation"]
    assert initial["tool"] == "read_file"
    assert initial["finished_at"] == store.job(first, "owner")["updated"]
    for prefix in ("card-observe:", "manage-observe:", "watch:"):
        complete("read_process", prefix + first)
    assert management.describe(current_task)["liveness"]["conversation"] == initial
    store.start_tool_activity(
        current_task["id"], "mcp-call", "task_get", next_timestamp(), window="chat"
    )
    assert management.describe(current_task)["liveness"]["conversation"]["tool"] == "task_get"
    complete("write_file", "cli-call")
    assert management.describe(current_task)["liveness"]["conversation"]["tool"] == "write_file"
