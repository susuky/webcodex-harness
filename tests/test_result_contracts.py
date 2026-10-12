from __future__ import annotations

import json

import httpx
import pytest

from webcodex.config import Client, ServerConfig
from webcodex.server import create_app
from webcodex.store import Store


@pytest.fixture
def completion_app(tmp_path):
    app = create_app(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            runners={"runner": "r" * 40},
            clients={
                "owner": Client(
                    token="c" * 40, projects=["runner/project"], approval_required=set()
                )
            },
        )
    )
    store = app.state.service.store
    store.register("runner", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "runner/project", "completion contract")
    job = store.enqueue(task, "read_process", {}, "once", False, 10)
    store.claim("runner", "instance", 1)
    yield app, task, job
    store.close()


@pytest.mark.parametrize(
    "result",
    [
        [],
        [1],
        None,
        "text",
        {"registered": "true"},
        {"registered": True},
        {"registered": True, "project": "not-a-project-id"},
        {"project": ["runner/project"]},
        {"process_id": ["process"]},
        {"process_id": "process", "status": "bogus"},
        {"status": ["running"]},
        {"exit_code": True},
        {"exit_code": "0"},
        {"exit_code": 2**63},
        {"has_more": "false"},
        {"next_cursor": True},
        {"next_cursor": -1},
        {"next_cursor": "0"},
    ],
)
async def test_invalid_results_are_rejected_before_persistence(completion_app, result):
    app, _, job = completion_app
    store = app.state.service.store
    with pytest.raises(ValueError, match="Invalid Runner completion"):
        store.complete("runner", job["job_id"], "instance", "succeeded", result)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8765",
    ) as client:
        response = await client.post(
            "/api/runner/complete",
            headers={"Authorization": "Bearer " + "r" * 40},
            json={
                "job_id": job["job_id"],
                "instance": "instance",
                "status": "succeeded",
                "result": result,
            },
        )
        assert response.status_code == 400
        response = await client.post(
            "/api/call",
            headers={"Authorization": "Bearer " + "c" * 40},
            json={"name": "job_get", "arguments": {"job_id": job["job_id"]}},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"
    assert store.job(job["job_id"], "owner")["result"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": []},
        {"job_id": ""},
        {"instance": 1},
        {"instance": "../instance"},
        {"status": "running"},
        {"unexpected": True},
    ],
)
async def test_completion_envelope_is_strict(completion_app, change):
    app, _, job = completion_app
    body = {"job_id": job["job_id"], "instance": "instance", "status": "succeeded", "result": {}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8765",
    ) as client:
        response = await client.post(
            "/api/runner/complete",
            headers={"Authorization": "Bearer " + "r" * 40},
            json={**body, **change},
        )
    assert response.status_code == 400
    assert app.state.service.store.job(job["job_id"], "owner")["status"] == "running"


async def test_completion_preserves_payload_ack_and_instance_fences(completion_app):
    app, task, job = completion_app
    store = app.state.service.store
    result = {
        "process_id": "process",
        "status": "running",
        "exit_code": None,
        "has_more": True,
        "next_cursor": 65536,
        "output": "完整輸出",
        "provider": {"opaque": [1, 2]},
    }
    for runner, instance in [("other", "instance"), ("runner", "other")]:
        with pytest.raises(ValueError, match="Runner instance"):
            store.complete(runner, job["job_id"], instance, "succeeded", result)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.post(
            "/api/runner/complete",
            headers={"Authorization": "Bearer " + "r" * 40},
            json={
                "job_id": job["job_id"],
                "instance": "instance",
                "status": "succeeded",
                "result": result,
            },
        )
    assert response.status_code == 200
    store.complete("runner", job["job_id"], "instance", "failed", {"error": "late retry"})
    saved = store.job(job["job_id"], "owner")
    assert saved["status"] == "succeeded" and saved["result"] == result
    assert saved["next_action"]["arguments"]["cursor"] == 65536
    activity = store.task_activity(task)
    assert activity["process_observations"][0]["process_id"] == "process"
    assert activity["latest_execution"]["result"] == result
    assert "process_status" not in saved


@pytest.mark.parametrize(
    "raw_result",
    [
        "not-json",
        "null",
        "[]",
        "[1]",
        '{"output":NaN}',
        json.dumps({"registered": True}),
        json.dumps({"process_id": 1}),
    ],
)
async def test_legacy_invalid_results_remain_readable_without_replay(completion_app, raw_result):
    app, task, job = completion_app
    store = app.state.service.store
    with store.db:
        store.db.execute(
            "UPDATE jobs SET status='succeeded',result=? WHERE id=?", (raw_result, job["job_id"])
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8765",
        headers={"Authorization": "Bearer " + "c" * 40},
    ) as client:
        response = await client.post(
            "/api/call", json={"name": "task_get", "arguments": {"task_id": task["id"]}}
        )
        assert response.status_code == 200
        response = await client.post(
            "/api/call", json={"name": "job_get", "arguments": {"job_id": job["job_id"]}}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert response.json()["result"]["error_type"] == "InvalidStoredResult"
    assert store.one("SELECT result FROM jobs WHERE id=?", (job["job_id"],)) is not None
    assert store.claim("runner", "instance", 1) == []


async def test_non_finite_completion_is_rejected_before_persistence(completion_app):
    app, _, job = completion_app
    result = {"output": float("nan")}
    store = app.state.service.store
    with pytest.raises(ValueError, match="Invalid Runner completion"):
        store.complete("runner", job["job_id"], "instance", "succeeded", result)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8765",
    ) as client:
        response = await client.post(
            "/api/runner/complete",
            headers={
                "Authorization": "Bearer " + "r" * 40,
                "Content-Type": "application/json",
            },
            content=json.dumps(
                {
                    "job_id": job["job_id"],
                    "instance": "instance",
                    "status": "succeeded",
                    "result": result,
                }
            ),
        )
    assert response.status_code == 400
    assert store.job(job["job_id"], "owner")["result"] is None


def test_non_process_status_and_registered_project_results_are_supported(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "other results")
        for index, result in enumerate(
            [
                {"status": " M arbitrary.txt\n"},
                {"registered": True, "project": "runner/new-project"},
            ]
        ):
            job = store.enqueue(task, "git_status", {}, str(index), False, 10)
            store.claim("runner", "instance", 1)
            store.complete("runner", job["job_id"], "instance", "succeeded", result)
            assert store.job(job["job_id"], "owner")["result"] == result
        assert store.task_activity(task)["process_observations"] == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "tool,result",
    [
        ("write_stdin", {"process_id": "process", "input_sent": True}),
        ("resize_process", {"process_id": "process", "rows": 24, "columns": 80}),
        ("read_process", {"process_id": "process"}),
    ],
)
def test_process_control_results_do_not_require_or_create_a_status(tmp_path, tool, result):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "process control")
        job = store.enqueue(task, tool, {}, "once", False, 10)
        store.claim("runner", "instance", 1)
        store.complete("runner", job["job_id"], "instance", "succeeded", result)
        saved = store.job(job["job_id"], "owner")
        assert saved["result"] == result
        assert "next_action" not in saved
        assert store.task_activity(task)["process_observations"] == []
        assert store.one("SELECT process_id FROM jobs WHERE id=?", (job["job_id"],)) == {
            "process_id": None
        }
    finally:
        store.close()
