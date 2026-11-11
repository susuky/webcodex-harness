from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from webcodex.config import Client, ManagementSource, ServerConfig
from webcodex.management import Management, execution_freshness
from webcodex.management_sources import Sources, priority
from webcodex.server import create_app
from webcodex.store import INTERNAL_OBSERVATION_PREFIXES

pytest_plugins = ["test_harness", "test_task_liveness"]


def scoped_request(source="local", query="", owner="owner", task_id=None):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/manage/sources/" + source,
            "query_string": query.encode(),
            "state": {"owner": owner},
            "path_params": {"source_id": source, **({"task_id": task_id} if task_id else {})},
        }
    )


def portal_config(path, sources):
    return ServerConfig(
        state_dir=path,
        admin_token="admin" * 8,
        runners={},
        clients={
            "owner": Client(
                token="portal" * 8, projects=[], permissions={"read"}, management_sources=sources
            ),
            "other": Client(token="other" * 8, projects=[], permissions={"read"}),
        },
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://internal.example",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "https://example.com#fragment",
        "http://127.0.0.1:0",
        "https://example.com:65536",
    ],
)
def test_management_source_configuration_rejects_unsafe_origins(url):
    with pytest.raises(ValidationError):
        ManagementSource(url=url, token="secret" * 8)


def test_management_sources_are_explicit_per_client_and_hide_tokens_in_repr(tmp_path):
    source = ManagementSource(url="https://example.com/", token="secret" * 8, label=" Windows ")
    assert source.url == "https://example.com" and source.label == "Windows"
    assert source.token not in repr(source)
    config = portal_config(tmp_path, {"windows": source})
    assert not config.clients["other"].management_sources
    for name in ("local", "../remote", "https://example.com"):
        with pytest.raises(ValidationError):
            Client(token="c" * 40, projects=[], management_sources={name: source})
    with pytest.raises(ValidationError):
        ManagementSource(url="https://example.com", token="x" * 40 + "\n")


async def test_portal_observes_real_legacy_server_and_handoff_is_read_only(harness, tmp_path):
    source = ManagementSource(url=harness.url, token="c" * 40, label="Windows")
    app = create_app(portal_config(tmp_path / "portal", {"windows": source}))
    remote = harness.app.state.service
    async with harness.http() as client:
        created = await client.post(
            "/api/call",
            json={
                "name": "task_create",
                "arguments": {"project": "local/project", "title": "Windows 長工作"},
            },
        )
        task_id = created.json()["id"]
        response = await client.post(
            "/api/call",
            json={
                "name": "exec_command",
                "arguments": {
                    "task_id": task_id,
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; print('started',flush=True); time.sleep(.5); print('finished')",
                    ],
                    "yield_time_ms": 0,
                },
            },
        )
        execution = response.json()
        deadline = time.monotonic() + 5
        while execution.get("status") in {"queued", "running"} and time.monotonic() < deadline:
            execution = (
                await client.post(
                    "/api/call",
                    json={
                        "name": "job_get",
                        "arguments": {"job_id": execution["job_id"], "wait_seconds": 1},
                    },
                )
            ).json()
        assert execution["result"]["status"] == "running"

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "portal" * 8},
        ) as portal:
            listed = (await portal.get("/api/manage/sources")).json()
            assert listed == {
                "sources": [
                    {
                        "id": "windows",
                        "label": "Windows",
                        "runner_labels": {},
                        "can_update_tasks": False,
                    }
                ]
            }
            assert source.token not in json.dumps(listed)
            state = (await portal.get("/api/manage/sources/windows/state")).json()
            assert state["tasks"][0]["id"] == task_id
            assert state["tasks"][0]["source_id"] == "windows"
            assert state["runners"][0]["source_label"] == "Windows"

            base = "/api/manage/sources/windows/tasks/" + task_id
            detail = (await portal.get(base)).json()
            observed = detail["processes"][0]["observed_at"]
            assert (
                observed
                == remote.store.task_activity(remote.store.task(task_id, "owner"))[
                    "process_observations"
                ][0]["observed_at"]
            )
            jobs_before = remote.store.one("SELECT count(*) AS n FROM jobs")["n"]
            controller_before = remote.store.rows("SELECT * FROM task_controllers")
            handoff = (await portal.get(base + "/handoff")).json()
            assert handoff["handoff"]["goal"] == "Windows 長工作"
            assert (
                task_id in handoff["continuation_prompt"]
                and "task_takeover" in handoff["continuation_prompt"]
            )
            assert remote.store.one("SELECT count(*) AS n FROM jobs")["n"] == jobs_before
            assert remote.store.rows("SELECT * FROM task_controllers") == controller_before

            task_before = remote.store.task(task_id, "owner")
            archived = await portal.post(base + "/archive", json={"archived": True})
            assert archived.status_code == 200 and archived.json()["archived"] is True
            assert (await portal.get("/api/manage/sources/windows/state")).json()["tasks"] == []
            archived_view = (
                await portal.get("/api/manage/sources/windows/state?archived=archived")
            ).json()
            assert [row["id"] for row in archived_view["tasks"]] == [task_id]
            assert (await portal.get(base)).json()["archived"] is True
            assert remote.store.task(task_id, "owner") == task_before
            assert remote.store.one("SELECT count(*) AS n FROM jobs")["n"] == jobs_before
            assert remote.store.rows("SELECT * FROM task_controllers") == controller_before
            restored = await portal.post(base + "/archive", json={"archived": False})
            assert restored.status_code == 200 and restored.json()["archived"] is False
            assert (await portal.get("/api/manage/sources/windows/state")).json()[
                "total_count"
            ] == 1

            await asyncio.sleep(0.6)
            refreshed = await portal.post(
                "/api/manage/sources/windows/refresh", json={"task_id": task_id}
            )
            assert refreshed.status_code == 200 and refreshed.json()["updates"]
            detail = (await portal.get(base)).json()
            assert detail["processes"][0]["status"] == "completed"
            assert "finished" in detail["outputs"][0]["output"]
            assert (
                remote.store.one("SELECT count(*) AS n FROM jobs WHERE tool='exec_command'")["n"]
                == 1
            )

            assert (
                await portal.get(
                    "/api/manage/sources/windows/state",
                    headers={"Authorization": "Bearer " + "other" * 8},
                )
            ).status_code == 400
            assert (
                await portal.get("/api/manage/sources", headers={"Authorization": ""})
            ).status_code == 401
            assert (
                await portal.get("/api/manage/sources/windows/state?url=https://other.invalid")
            ).status_code == 400
            assert (
                await portal.post(
                    "/api/manage/sources/windows/refresh", json={"tool": "exec_command"}
                )
            ).status_code == 400
    finally:
        await app.state.management_sources.close()
        app.state.service.store.close()
        app.state.service.diagnostics.close()


async def test_mark_complete_and_reopen_updates_source_and_preserves_notes(harness, tmp_path):
    source = ManagementSource(url=harness.url, token="c" * 40, label="Windows")
    config = portal_config(tmp_path / "status-portal", {"windows": source})
    app = create_app(config)
    store = harness.app.state.service.store
    task = store.create_task("owner", "local/project", "狀態操作")
    plan = [{"step": "保留原步驟", "status": "in_progress"}]
    store.update_task(task["id"], "owner", "waiting", "保留原摘要", plan)
    # A status annotation does not cancel an already queued or running command.
    job = store.enqueue(task, "exec_command", {}, "status-test", 100)
    base = "/api/manage/sources/windows/tasks/" + task["id"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "portal" * 8},
        ) as portal:
            assert (
                await portal.post(base + "/status", json={"status": "completed"})
            ).status_code == 403
            config.clients["owner"].permissions.add("write")
            assert (await portal.get("/api/manage/sources")).json()["sources"][0][
                "can_update_tasks"
            ]
            assert (
                await portal.post(base + "/status", json={"status": "deleted"})
            ).status_code == 400
            assert (
                await portal.post(
                    base + "/status", json={"status": "completed", "summary": "overwrite"}
                )
            ).status_code == 400
            await portal.get("/api/manage/sources/windows/state")
            await portal.post(base + "/archive", json={"archived": True})
            for status in ("completed", "active"):
                response = await portal.post(base + "/status", json={"status": status})
                assert response.status_code == 200 and response.json()["status"] == status
                actual = store.task(task["id"], "owner")
                assert actual["status"] == status
                assert actual["summary"] == "保留原摘要" and actual["plan"] == plan
                archived = (
                    await portal.get("/api/manage/sources/windows/state?archived=all")
                ).json()
                assert archived["tasks"][0]["status"] == status
                assert archived["tasks"][0]["archived"] is True
                assert store.job(job["job_id"], "owner")["status"] != "cancelled"
            other = store.create_task("reviewer", "local/project", "其他帳號")
            denied = await portal.post(
                "/api/manage/sources/windows/tasks/" + other["id"] + "/status",
                json={"status": "completed"},
            )
            assert denied.status_code == 404
            assert store.task(other["id"], "reviewer")["status"] == "active"
            harness.config.clients["owner"].permissions.discard("write")
            assert (
                await portal.post(base + "/status", json={"status": "completed"})
            ).status_code == 403
    finally:
        await app.state.management_sources.close()
        app.state.service.store.close()
        app.state.service.diagnostics.close()

    # Local sources use the same write permission and original task_update behavior.
    harness.config.clients["owner"].permissions.add("write")
    async with harness.http() as client:
        assert (
            await client.post(
                "/api/manage/sources/local/tasks/missing/status",
                json={"status": "completed"},
            )
        ).status_code == 404
        response = await client.post(
            "/api/manage/sources/local/tasks/" + task["id"] + "/status",
            json={"status": "completed"},
        )
        assert response.status_code == 200
        assert store.task(task["id"], "owner")["summary"] == "保留原摘要"


async def test_observation_does_not_block_cached_catalog_or_invalidate_without_updates(
    activity_service, monkeypatch
):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url="https://windows.example", token="w" * 40)
    }
    manager = Sources(Management(service))
    catalog = {"tasks": [], "runners": [], "observed_at": time.time()}
    manager.catalogs[("owner", "windows")] = (time.monotonic(), catalog)
    entered, release = asyncio.Event(), asyncio.Event()
    updates = []
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        assert request.url.path == "/api/manage/refresh"
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"updates": updates, "observed_at": time.time()})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda *_: original_client(
            base_url="https://windows.example", transport=httpx.MockTransport(upstream)
        ),
    )

    def request(body):
        req = scoped_request("windows")
        req._json = body
        return req

    assert json.loads((await manager.refresh(request({"task_ids": []}))).body)["updates"] == []
    assert not calls
    refresh = asyncio.create_task(manager.refresh(request({"task_id": "idle"})))
    await entered.wait()
    try:
        assert await asyncio.wait_for(manager.remote_catalog("owner", "windows"), 0.5) is catalog
    finally:
        release.set()
        await refresh
    assert ("owner", "windows") in manager.catalogs
    updates.append({"process_id": "p", "status": "succeeded"})
    await manager.refresh(request({"task_id": "active"}))
    assert ("owner", "windows") not in manager.catalogs


async def test_local_catalog_prioritizes_old_work_searches_before_paging_and_keeps_handoff(
    activity_service,
):
    service, live = activity_service
    store = service.store
    for index in range(65):
        task = store.create_task("owner", "local/project", f"Completed {index}")
        store.update_task(task["id"], "owner", "completed", "", [])
    waiting = store.create_task("owner", "local/project", "Ready without execution")
    old = store.enqueue(live, "exec_command", {}, "old", 100)
    store.claim("local", "instance", 1)
    store.complete(
        "local",
        old["job_id"],
        "instance",
        "succeeded",
        {"process_id": old["job_id"], "status": "running"},
    )
    manager = Sources(Management(service))
    state = json.loads((await manager.state(scoped_request(query="limit=1"))).body)
    assert [task["id"] for task in state["tasks"]] == [live["id"]]
    assert state["next_offset"] == 1 and state["total_count"] == 67
    active = json.loads((await manager.state(scoped_request(query="state=active"))).body)
    assert [task["id"] for task in active["tasks"]] == [live["id"]]
    searched = json.loads(
        (await manager.state(scoped_request(query="q=Completed%2064&limit=1"))).body
    )
    assert [task["title"] for task in searched["tasks"]] == ["Completed 64"]
    assert searched["next_offset"] is None
    assert (
        json.loads((await manager.state(scoped_request(query="runner=missing"))).body)["tasks"]
        == []
    )
    before = store.one("SELECT count(*) AS n FROM jobs")["n"]
    handoff = json.loads((await manager.handoff(scoped_request(task_id=waiting["id"]))).body)
    assert handoff["handoff"]["goal"] == "Ready without execution"
    assert store.one("SELECT count(*) AS n FROM jobs")["n"] == before


async def test_remote_catalog_legacy_paging_source_collisions_and_failures(
    activity_service, monkeypatch
):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        name: ManagementSource(url="https://" + name + ".example", token=name * 40, label=name)
        for name in ("windows", "ubuntu", "offline", "redirect", "malformed", "revoked")
    }
    manager = Sources(Management(service))
    seen = []
    now = time.time()

    def task(title, index=0, running=False):
        return {
            "id": "same-id" if running else f"task-{index}",
            "title": title,
            "runner": "local",
            "project": "project",
            "status": "active" if running else "completed",
            "state": "observing" if running else "completed",
            "updated": now - index,
            "processes": [{"status": "running", "process_id": "process", "observed_at": now - 3600}]
            if running
            else [],
            "jobs": [],
            "liveness": {"work": {"running_count": int(running)}},
        }

    async def upstream(request):
        seen.append(
            (
                request.url.host,
                request.url.path,
                dict(request.url.params),
                request.headers["authorization"],
            )
        )
        host = request.url.host.split(".", 1)[0]
        if host == "offline":
            raise httpx.ConnectError("private connection details", request=request)
        if host == "redirect":
            return httpx.Response(
                302, headers={"location": "https://unconfigured.example/api/call"}
            )
        if host == "malformed":
            return httpx.Response(200, json={"tasks": []})
        if host == "revoked":
            return httpx.Response(401, json={"error": "secret upstream response"})
        assert request.url.path == "/api/manage/state"
        if request.url.params.get("offset", "0") == "0":
            tasks, next_offset = (
                [task(host + " done " + str(index), index) for index in range(60)],
                60,
            )
        else:
            tasks, next_offset = [task(host + " old running", 100, True)], None
        return httpx.Response(
            200,
            json={
                "tasks": tasks,
                "runners": [{"id": "local", "online": True, "last_seen": now}],
                "next_offset": next_offset,
                "observed_at": now,
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda owner, source_id: original_client(
            base_url=manager.source(owner, source_id).url,
            headers={"Authorization": "Bearer " + manager.source(owner, source_id).token},
            transport=httpx.MockTransport(upstream),
            follow_redirects=False,
        ),
    )
    windows = json.loads((await manager.state(scoped_request("windows", "limit=1"))).body)
    ubuntu = json.loads((await manager.state(scoped_request("ubuntu", "limit=1"))).body)
    assert windows["tasks"][0]["id"] == ubuntu["tasks"][0]["id"] == "same-id"
    assert (
        windows["tasks"][0]["source_id"] == "windows"
        and ubuntu["tasks"][0]["source_id"] == "ubuntu"
    )
    assert windows["tasks"][0]["processes"][0]["observed_at"] == now - 3600
    assert windows["tasks"][0]["processes"][0]["display_status"] == "unconfirmed"
    search = json.loads((await manager.state(scoped_request("windows", "q=old%20running"))).body)
    assert search["total_count"] == 1 and search["next_offset"] is None
    assert (
        len(seen) == 4
    )  # The complete catalogs serve subsequent filters during the short cache window.
    active = json.loads((await manager.state(scoped_request("windows", "state=active"))).body)
    unconfirmed = json.loads(
        (await manager.state(scoped_request("windows", "state=unconfirmed"))).body
    )
    assert active["total_count"] == 0 and unconfirmed["total_count"] == 1
    for name in ("offline", "redirect", "malformed", "revoked"):
        response = await manager.state(scoped_request(name))
        error = json.loads(response.body)
        assert response.status_code == 502 and error["source_id"] == name
        assert "private" not in error["error"] and "secret" not in error["error"]
    assert json.loads((await manager.state(scoped_request("windows"))).body)["total_count"] == 61
    assert all(path == "/api/manage/state" for _, path, _, _ in seen)
    assert all(host.split(".", 1)[0] * 40 in authorization for host, _, _, authorization in seen)
    with pytest.raises(ValueError, match="not available"):
        await manager.state(scoped_request("windows", owner="other"))


@pytest.mark.parametrize("online", [True, False])
async def test_remote_pages_share_latest_runner_connection_without_refreshing_work(
    activity_service, monkeypatch, online
):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url="https://windows.example", token="s" * 40)
    }
    manager = Sources(Management(service))
    now = time.time()
    seen = []
    task = {
        "id": "first-page-task",
        "title": "Original work",
        "runner": "local",
        "project": "project",
        "status": "active",
        "state": "observing" if not online else "waiting_runner",
        "runner_online": not online,
        "updated": now - 60,
        "processes": [
            {"status": "running", "process_id": "old-process", "observed_at": now - 3600}
        ],
        "jobs": [{"job_id": "queued-job", "tool": "read_file", "status": "queued"}],
        "liveness": {"observed_at": now - 5, "work": {"running_count": 1, "pending_count": 1}},
    }

    async def upstream(request):
        offset = request.url.params["offset"]
        seen.append(offset)
        return httpx.Response(
            200,
            json={
                "tasks": [task]
                if offset == "0"
                else [{**task, "id": "other-runner", "runner": "course-hub"}],
                "runners": [
                    {
                        "id": "local",
                        "online": not online if offset == "0" else online,
                        "last_seen": now,
                    },
                    {"id": "course-hub", "online": not online, "last_seen": now - 30},
                ],
                "next_offset": 1 if offset == "0" else None,
                "observed_at": now - 5 if offset == "0" else now,
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda *_: original_client(
            base_url="https://windows.example", transport=httpx.MockTransport(upstream)
        ),
    )
    state = json.loads((await manager.state(scoped_request("windows"))).body)
    rows = {row["id"]: row for row in state["tasks"]}
    first = rows["first-page-task"]
    assert seen == ["0", "1"]
    assert first["runner_online"] is state["runners"][0]["online"] is online
    assert first["state"] == ("observing" if online else "waiting_runner")
    assert first["liveness"]["runner_last_seen"] == now
    assert first["liveness"]["runner_observed_at"] == now
    assert first["liveness"]["observed_at"] == now - 5
    assert first["processes"][0]["observed_at"] == now - 3600
    assert first["processes"][0]["display_status"] == "unconfirmed"
    assert first["liveness"]["work"]["unconfirmed_count"] == 1
    assert rows["other-runner"]["runner_online"] is not online
    assert rows["other-runner"]["state"] == ("waiting_runner" if online else "observing")
    assert task["runner_online"] is not online


async def test_source_cache_keeps_two_client_grants_separate(activity_service, monkeypatch):
    service, _ = activity_service
    for owner in ("owner", "other"):
        service.config.clients[owner].management_sources = {
            "windows": ManagementSource(url="https://same.example", token=owner * 40)
        }
    manager = Sources(Management(service))
    seen = []

    async def upstream(request):
        token = request.headers["authorization"]
        seen.append(token)
        title = "Owner task" if token == "Bearer " + "owner" * 40 else "Other task"
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "same-id",
                        "title": title,
                        "runner": "local",
                        "project": "project",
                        "status": "completed",
                        "updated": 1,
                    }
                ],
                "runners": [],
                "next_offset": None,
                "observed_at": 1,
            },
        )

    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda owner, source_id: httpx.AsyncClient(
            base_url="https://same.example",
            transport=httpx.MockTransport(upstream),
            headers={"Authorization": "Bearer " + manager.source(owner, source_id).token},
        ),
    )
    first, other, repeated = await asyncio.gather(
        *(
            manager.state(scoped_request("windows", owner=owner))
            for owner in ("owner", "other", "owner")
        )
    )
    assert json.loads(first.body)["tasks"][0]["title"] == "Owner task"
    assert json.loads(other.body)["tasks"][0]["title"] == "Other task"
    assert json.loads(repeated.body)["tasks"][0]["title"] == "Owner task"
    assert len(seen) == 2


@pytest.mark.parametrize("missing_status", [400, 404, 401, 403])
async def test_missing_remote_task_does_not_hide_other_tasks(
    activity_service, monkeypatch, missing_status
):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url="https://windows.example", token="w" * 40)
    }
    manager = Sources(Management(service))
    task = {
        "id": "available",
        "title": "Available task",
        "runner": "local",
        "project": "project",
        "status": "active",
        "updated": 1,
        "created_at": 1,
    }

    async def upstream(request):
        if request.url.path == "/api/manage/state":
            return httpx.Response(
                200, json={"tasks": [task], "runners": [], "next_offset": None, "observed_at": 1}
            )
        if request.url.path == "/api/manage/tasks/missing":
            return httpx.Response(missing_status, json={"error": "Task not found"})
        assert request.url.path == "/api/manage/tasks/available"
        return httpx.Response(200, json=task)

    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda owner, source_id: httpx.AsyncClient(
            base_url="https://windows.example",
            transport=httpx.MockTransport(upstream),
        ),
    )
    assert (await manager.state(scoped_request("windows"))).status_code == 200
    expected = 404 if missing_status in {400, 404} else 502
    missing = await manager.detail(scoped_request("windows", task_id="missing"))
    assert missing.status_code == expected
    if expected == 404:
        assert json.loads(missing.body)["error"] == "任務不存在或無權存取"
    assert (
        await manager.handoff(scoped_request("windows", task_id="missing"))
    ).status_code == expected
    assert (await manager.state(scoped_request("windows"))).status_code == 200
    available = await manager.detail(scoped_request("windows", task_id="available"))
    assert available.status_code == 200 and json.loads(available.body)["id"] == "available"


async def test_missing_or_revoked_local_task_returns_not_found(activity_service):
    service, task = activity_service
    manager = Sources(Management(service))
    assert (await manager.detail(scoped_request(task_id="missing"))).status_code == 404
    assert (await manager.handoff(scoped_request(task_id="missing"))).status_code == 404
    service.config.clients["owner"].projects = []
    assert (await manager.detail(scoped_request(task_id=task["id"]))).status_code == 404
    assert (await manager.handoff(scoped_request(task_id=task["id"]))).status_code == 404
    assert (await manager.state(scoped_request())).status_code == 200


async def test_local_catalog_reads_projection_with_large_history(activity_service):
    service, task = activity_service
    store = service.store
    with store.db:
        store.db.executemany(
            "INSERT INTO jobs(id,task_id,owner,runner,project,binding,tool,arguments,status,request_id,created,updated,process_id,process_status) "
            "VALUES (?,?,?,?,?,?,'read_process','{}','succeeded',NULL,?,?,?,'completed')",
            (
                (
                    f"history-{index:06}",
                    task["id"],
                    "owner",
                    "local",
                    "project",
                    task["binding"],
                    index,
                    index,
                    "finished-command",
                )
                for index in range(106000)
            ),
        )
    queued = store.enqueue(task, "read_file", {}, "pending", 100)
    queries = []
    store.db.set_trace_callback(queries.append)
    try:
        catalog = Sources(Management(service)).local_catalog("owner")
    finally:
        store.db.set_trace_callback(None)
    assert (
        catalog[0]["_priority"] == 0 and store.job(queued["job_id"], "owner")["status"] == "queued"
    )
    assert not any("result" in query.casefold() for query in queries)


def test_stale_running_is_unconfirmed_without_rewriting_saved_status():
    task = {
        "created_at": 100,
        "state": "observing",
        "runner_online": True,
        "processes": [
            {"process_id": "old", "status": "running", "observed_at": 200},
            {"process_id": "new", "status": "running", "observed_at": 990},
            {"process_id": "done", "status": "completed", "observed_at": 300},
        ],
        "liveness": {"work": {"running_count": 2, "pending_count": 0}},
    }
    fresh = execution_freshness(task, now=1000)
    assert [p["display_status"] for p in fresh["processes"]] == [
        "unconfirmed",
        "running",
        "completed",
    ]
    assert fresh["liveness"]["work"]["confirmed_running_count"] == 1
    assert fresh["liveness"]["work"]["unconfirmed_count"] == 1
    assert fresh["state"] == "observing"
    later = execution_freshness(task, now=1200)
    assert later["state"] == "needs_confirmation"
    assert later["liveness"]["work"]["confirmed_running_count"] == 0
    assert later["liveness"]["work"]["unconfirmed_count"] == 2
    assert "display_status" not in task["processes"][0]
    assert task["processes"][0]["status"] == "running"
    offline = execution_freshness({**task, "runner_online": False}, now=1000)
    assert offline["state"] == "needs_confirmation"


def test_task_times_keep_creation_separate_from_first_real_work(activity_service):
    service, task = activity_service
    store = service.store
    manager = Management(service)
    empty = manager.describe(task)
    assert empty["created_at"] == task["created"] and empty["started_at"] is None
    internal = store.enqueue(task, "read_process", {}, "manage-observe:only", 100)
    assert manager.describe(task)["started_at"] is None
    real = store.enqueue(task, "workspace_info", {}, "first-real-work", 100)
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        view = manager.describe(task)
    finally:
        store.db.set_trace_callback(None)
    assert view["started_at"] == store.job(real["job_id"], "owner")["created"]
    assert view["started_at"] >= store.job(internal["job_id"], "owner")["created"]
    query = next(sql for sql in statements if "SELECT created AS started_at" in sql)
    assert any(
        "task_job_history" in row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + query)
    )


def test_finished_legacy_agent_is_not_resurrected_as_an_unconfirmed_process(activity_service):
    service, task = activity_service
    store = service.store

    def complete(tool, result):
        job = store.enqueue(task, tool, {}, None, 100)
        store.claim("local", "instance", 100)
        store.complete("local", job["job_id"], "instance", "succeeded", result)

    complete("subagent_spawn", {"agent_id": "old-agent", "status": "running"})
    complete("read_process", {"process_id": "old-agent", "status": "running"})
    complete("subagent_get", {"agent_id": "old-agent", "status": "completed", "exit_code": 0})
    catalog = Sources(Management(service)).local_catalog("owner")
    assert catalog[0]["_priority"] != 0 and not catalog[0]["_unconfirmed"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"processes": [{"status": "running", "observed_at": "yesterday"}]},
        {"liveness": {"work": {"running_count": None}}},
        {"created_at": "2026/11/5"},
    ],
)
async def test_invalid_freshness_data_is_a_source_error(activity_service, monkeypatch, invalid):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url="https://windows.example", token="w" * 40)
    }
    manager = Sources(Management(service))
    task = {
        "id": "task",
        "title": "Task",
        "runner": "local",
        "project": "project",
        "status": "active",
        "updated": 1,
        **invalid,
    }

    async def upstream(request):
        payload = (
            {"tasks": [task], "runners": [], "next_offset": None, "observed_at": 1}
            if request.url.path.endswith("/state")
            else task
        )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda owner, source_id: httpx.AsyncClient(
            base_url="https://windows.example",
            transport=httpx.MockTransport(upstream),
        ),
    )
    state = await manager.state(scoped_request("windows"))
    detail = await manager.detail(scoped_request("windows", task_id="task"))
    assert state.status_code == detail.status_code == 502
    assert json.loads(state.body)["source_id"] == "windows"


async def test_legacy_creation_event_is_cached_without_changing_task_activity(harness, monkeypatch):
    service = harness.app.state.service
    task = service.store.create_task("owner", "local/project", "Legacy creation date")
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url=harness.url, token="c" * 40)
    }
    legacy = next(
        route.endpoint.__self__ for route in harness.app.routes if route.path == "/api/manage/state"
    )
    original_describe = legacy.describe

    def old_describe(task):
        result = original_describe(task)
        result.pop("created_at", None)
        result.pop("started_at", None)
        return result

    monkeypatch.setattr(legacy, "describe", old_describe)
    service.store.start_tool_activity(
        task["id"], "model-trace", "read_file", time.time(), window="model-chat"
    )
    service.store.finish_tool_activity(
        task["id"],
        "model-trace",
        service.store.tool_activity(task["id"], "model-chat")["started_at"],
        "returned",
    )
    tables = (
        "tasks",
        "jobs",
        "events",
        "task_tool_activity",
        "task_controllers",
        "task_waits",
        "task_deliveries",
    )
    before = {name: service.store.rows("SELECT * FROM " + name) for name in tables}
    traced = service.traced_call
    calls = []

    async def observe(owner, name, arguments, **kwargs):
        assert name != "task_get"
        calls.append((name, arguments, kwargs.get("track_activity", False)))
        return await traced(owner, name, arguments, **kwargs)

    monkeypatch.setattr(service, "traced_call", observe)
    manager = Sources(Management(service))
    state = json.loads((await manager.state(scoped_request("windows"))).body)
    assert state["tasks"][0]["created_at"] is None
    assert calls == []
    first = json.loads((await manager.detail(scoped_request("windows", task_id=task["id"]))).body)
    event_time = service.store.one(
        "SELECT created FROM events WHERE task_id=? AND kind='task.created'", (task["id"],)
    )["created"]
    assert first["created_at"] == event_time and first["started_at"] is None
    await manager.detail(scoped_request("windows", task_id=task["id"]))
    await manager.handoff(scoped_request("windows", task_id=task["id"]))
    cached_list = json.loads((await manager.state(scoped_request("windows"))).body)
    assert cached_list["tasks"][0]["created_at"] == event_time
    assert calls == [("task_events", {"task_id": task["id"], "after": 0, "limit": 1}, False)]
    assert {name: service.store.rows("SELECT * FROM " + name) for name in tables} == before


async def test_missing_creation_event_stays_unknown_without_expensive_fallback(
    activity_service, monkeypatch
):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(url="https://windows.example", token="w" * 40)
    }
    manager = Sources(Management(service))
    task = {
        "id": "task",
        "title": "Legacy",
        "runner": "local",
        "project": "project",
        "status": "active",
        "updated": 1,
    }
    calls = []

    async def upstream(request):
        if request.method == "POST":
            body = json.loads(request.content)
            calls.append(body)
            assert request.url.path == "/api/call" and body == {
                "name": "task_events",
                "arguments": {"task_id": "task", "after": 0, "limit": 1},
            }
            return httpx.Response(200, json={"events": [], "next_cursor": 0})
        return httpx.Response(200, json=task)

    monkeypatch.setattr(
        manager,
        "remote_client",
        lambda owner, source_id: httpx.AsyncClient(
            base_url="https://windows.example",
            transport=httpx.MockTransport(upstream),
        ),
    )
    for _ in range(2):
        response = await manager.detail(scoped_request("windows", task_id="task"))
        data = json.loads(response.body)
        assert (
            response.status_code == 200
            and data["created_at"] is None
            and data["started_at"] is None
        )
    assert len(calls) == 1


def test_catalog_does_not_count_internal_observation_jobs_as_active(activity_service):
    service, task = activity_service
    for prefix in INTERNAL_OBSERVATION_PREFIXES:
        service.store.enqueue(task, "read_process", {}, prefix + "queued", 100)
    manager = Sources(Management(service))
    assert manager.local_catalog("owner")[0]["_priority"] != 0
    service.store.enqueue(task, "read_process", {}, "user-requested-read", 100)
    assert manager.local_catalog("owner")[0]["_priority"] == 0


async def test_archive_is_persistent_viewer_preference_while_source_is_offline(tmp_path):
    source = ManagementSource(url="https://offline.invalid", token="s" * 40)
    config = portal_config(tmp_path / "portal", {"windows": source, "ubuntu": source})
    config.clients["other"].management_sources = {"windows": source}
    app = create_app(config)
    base = "/api/manage/sources/windows/tasks/same-id/archive"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "portal" * 8},
        ) as client:
            response = await client.post(base, json={"archived": True})
            assert response.status_code == 200 and response.json()["archived"] is True
            for body in ({"archived": "yes"}, {"archived": True, "status": "completed"}, {}):
                assert (await client.post(base, json=body)).status_code == 400
            assert (
                await client.post(base, json={"archived": False}, headers={"Authorization": ""})
            ).status_code == 401
            assert (
                await client.post(base.replace("windows", "missing"), json={"archived": True})
            ).status_code == 400
    finally:
        await app.state.management_sources.close()
        app.state.service.store.close()
        app.state.service.diagnostics.close()
    reopened = create_app(config)
    try:
        manager = Sources(Management(reopened.state.service))
        assert manager.archived_ids("owner", "windows") == {"same-id"}
        assert manager.archived_ids("other", "windows") == set()
        assert manager.archived_ids("owner", "ubuntu") == set()
        assert reopened.state.service.store.rows("SELECT * FROM tasks") == []
        assert reopened.state.service.store.rows("SELECT * FROM jobs") == []
    finally:
        await reopened.state.management_sources.close()
        reopened.state.service.store.close()
        reopened.state.service.diagnostics.close()


async def test_complete_project_options_and_environment_filters(activity_service, monkeypatch):
    service, _ = activity_service
    service.config.clients["owner"].management_sources = {
        "windows": ManagementSource(
            url="https://windows.example",
            token="s" * 40,
            label="Windows",
            runner_labels={"wsl": "Windows · WSL"},
        )
    }
    manager = Sources(Management(service))
    catalog = [
        {
            "id": str(i),
            "title": str(i),
            "runner": "local",
            "project": "native",
            "status": "active",
            "state": "needs_attention",
            "updated": i,
        }
        for i in range(65)
    ] + [
        {
            "id": "older",
            "title": "old",
            "runner": "wsl",
            "project": "linux",
            "status": "active",
            "state": "needs_attention",
            "updated": 0,
        }
    ]

    async def remote_catalog(owner, source_id):
        return {"tasks": catalog, "runners": [], "observed_at": time.time()}

    monkeypatch.setattr(manager, "remote_catalog", remote_catalog)
    first = json.loads((await manager.state(scoped_request("windows", "limit=1"))).body)
    assert len(first["tasks"]) == 1 and first["total_count"] == 66
    assert first["projects"] == [
        {"runner": "wsl", "project": "linux", "environment_label": "Windows · WSL"},
        {"runner": "local", "project": "native", "environment_label": "Windows"},
    ]
    filtered = json.loads(
        (await manager.state(scoped_request("windows", "project=linux&runner=wsl"))).body
    )
    assert [task["id"] for task in filtered["tasks"]] == ["older"]
    assert filtered["tasks"][0]["state"] == "ready"
    assert filtered["projects"] == first["projects"]
    native = json.loads(
        (await manager.state(scoped_request("windows", "environment=Windows"))).body
    )
    assert native["total_count"] == 65
    assert all(task["environment_label"] == "Windows" for task in native["tasks"])
    attention = json.loads((await manager.state(scoped_request("windows", "state=attention"))).body)
    assert attention["tasks"] == []


async def test_local_and_remote_priorities_agree_on_changed_project_and_old_approval(
    activity_service,
):
    service, task = activity_service
    store = service.store
    manager = Sources(Management(service))
    store.register("local", "instance", {"project": "new-binding"})
    changed = manager.management.describe(task)
    assert changed["state"] == "project_changed"
    assert priority(manager.local_catalog("owner")[0]) == priority(changed) == 1
    assert (
        json.loads((await manager.state(scoped_request(query="state=attention"))).body)[
            "total_count"
        ]
        == 1
    )

    store.register("local", "instance", {"project": task["binding"]})
    job = store.enqueue(task, "exec_command", {}, "legacy-approval", 100)
    with store.db:
        store.db.execute("UPDATE jobs SET status='pending_approval' WHERE id=?", (job["job_id"],))
    store.update_task(task["id"], "owner", "completed", "finished", [])
    completed = manager.management.describe(store.task(task["id"], "owner"))
    assert (
        completed["state"] == "completed" and completed["jobs"][0]["status"] == "pending_approval"
    )
    assert priority(manager.local_catalog("owner")[0]) == priority(completed) == 3
    assert (
        json.loads((await manager.state(scoped_request(query="state=active"))).body)["tasks"] == []
    )


@pytest.mark.parametrize("source_id", ["local", "windows"])
async def test_unfinished_and_completed_filters_intersect_archive_without_resurrecting_work(
    activity_service, monkeypatch, source_id
):
    service, live = activity_service
    store = service.store
    tasks = {"live": live}
    for name in (
        "idle",
        "attention",
        "stale",
        "done_live",
        "done_stale",
        "done_attention",
        "archived_unfinished",
        "archived_completed",
    ):
        tasks[name] = store.create_task("owner", "local/project", name)
    for name in ("live", "stale", "done_live", "done_stale"):
        job = store.enqueue(tasks[name], "exec_command", {}, name, 100)
        store.claim("local", "instance", 1)
        store.complete(
            "local",
            job["job_id"],
            "instance",
            "succeeded",
            {"process_id": job["job_id"], "status": "running"},
        )
        if name in {"stale", "done_stale"}:
            with store.db:
                store.db.execute(
                    "UPDATE jobs SET updated=? WHERE id=?", (time.time() - 3600, job["job_id"])
                )
    residual = store.enqueue(tasks["done_live"], "exec_command", {}, "still-delivering", 100)
    store.claim("local", "instance", 1)
    for name in ("attention", "done_attention", "live"):
        job = store.enqueue(tasks[name], "write_file", {}, "approval-" + name, 100)
        with store.db:
            store.db.execute(
                "UPDATE jobs SET status='pending_approval' WHERE id=?", (job["job_id"],)
            )
    store.update_task(tasks["idle"]["id"], "owner", "waiting", "waiting without tools", [])
    for name in ("done_live", "done_stale", "done_attention", "archived_completed"):
        store.update_task(tasks[name]["id"], "owner", "completed", "finished", [])

    if source_id == "windows":
        service.config.clients["owner"].management_sources = {
            "windows": ManagementSource(url="https://windows.example", token="w" * 40)
        }
    manager = Sources(Management(service))
    if source_id == "windows":
        # Exercise the legacy source HTTP catalog with real stored process/job projections.
        async def upstream(request):
            assert request.method == "GET" and request.url.path == "/api/manage/state"
            return httpx.Response(
                200,
                json={
                    "tasks": [
                        manager.management.describe(store.task(task["id"], "owner"))
                        for task in tasks.values()
                    ],
                    "runners": manager.management.runners("owner"),
                    "next_offset": None,
                    "observed_at": time.time(),
                },
            )

        monkeypatch.setattr(
            manager,
            "remote_client",
            lambda *_: httpx.AsyncClient(
                base_url="https://windows.example", transport=httpx.MockTransport(upstream)
            ),
        )
    for name in ("archived_unfinished", "archived_completed"):
        request = scoped_request(source_id, task_id=tasks[name]["id"])
        request._json = {"archived": True}
        assert (await manager.archive(request)).status_code == 200

    unfinished = {"live", "idle", "attention", "stale"}
    completed = {"done_live", "done_stale", "done_attention"}
    for state, archived, expected in (
        ("unfinished", "active", unfinished),
        ("unfinished", "archived", {"archived_unfinished"}),
        ("unfinished", "all", unfinished | {"archived_unfinished"}),
        ("completed", "active", completed),
        ("completed", "archived", {"archived_completed"}),
        ("completed", "all", completed | {"archived_completed"}),
        ("active", "active", {"live"}),
        ("attention", "active", {"attention", "live"}),
        ("unconfirmed", "active", {"stale"}),
        ("all", "active", unfinished | completed),
        ("all", "all", set(tasks)),
    ):
        response = await manager.state(
            scoped_request(source_id, query=f"state={state}&archived={archived}")
        )
        assert response.status_code == 200
        data = json.loads(response.body)
        assert {task["id"] for task in data["tasks"]} == {tasks[name]["id"] for name in expected}, (
            state,
            archived,
        )
        assert data["total_count"] == len(expected) and data["next_offset"] is None

    # Completion changes the task list classification, not the saved work observations.
    all_tasks = data["tasks"]
    assert [task["status"] == "completed" for task in all_tasks] == [False] * 5 + [True] * 4
    by_id = {task["id"]: task for task in all_tasks}
    assert priority(by_id[live["id"]]) == 0
    assert any(job["status"] == "pending_approval" for job in by_id[live["id"]]["jobs"])
    assert by_id[tasks["done_live"]["id"]]["processes"][0]["status"] == "running"
    assert by_id[tasks["done_stale"]["id"]]["liveness"]["work"]["unconfirmed_count"] == 1
    assert store.job(residual["job_id"], "owner")["status"] == "running"
    for name in completed | {"archived_completed"}:
        assert priority(by_id[tasks[name]["id"]]) == 3
    assert all(
        task["_priority"] == 3
        for task in manager.local_catalog("owner")
        if task["status"] == "completed"
    )
