from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time

import httpx
import pytest
from test_harness import call as mcp_call

from webcodex.computer import Computer
from webcodex.config import Client, ManagementSource, RunnerConfig, ServerConfig, initialize
from webcodex.extensions import Extensions
from webcodex.filesystem import Workspace
from webcodex.maintenance import retention
from webcodex.management import Management
from webcodex.management_auth import ManagementAuth
from webcodex.management_sources import Sources
from webcodex.processes import Processes
from webcodex.runtime import Runtime
from webcodex.server import REMOTE, Security, Service, create_app
from webcodex.store import TaskArchived

pytest_plugins = ["test_harness"]


def _has_pydantic_title(schema: object) -> bool:
    if isinstance(schema, dict):
        if isinstance(schema.get("title"), str):
            return True
        return any(_has_pydantic_title(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_has_pydantic_title(item) for item in schema)
    return False


async def test_schema_envelope_slimmed_and_same_turn_mandated(harness):
    async with harness.mcp() as client:
        init = await client.initialize()
        assert len(init.instructions) < 2500
        for required in (
            "task_create",
            "host execution blocked",
            "same turn",
            "job_get/read_process",
            "Automatic messages default off",
            "prohibited from starting Codex",
            "NEVER end the turn",
        ):
            assert required in init.instructions
        assert "next_action is task_wait" not in init.instructions

        listed = await client.list_tools()
        payload = [tool.model_dump(mode="json", by_alias=True) for tool in listed.tools]
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded) < 95_000
        for tool in payload:
            assert not _has_pydantic_title(tool["inputSchema"]), tool["name"]

        by_name = {tool["name"]: tool for tool in payload}
        assert "same turn" in by_name["job_get"]["description"].lower()
        assert "do not end the turn" in by_name["job_get"]["description"].lower()
        assert "same turn" in by_name["read_process"]["description"].lower()
        assert "do not end the turn" in by_name["read_process"]["description"].lower()

        task = await mcp_call(client, "task_create", {"project": "local/project", "title": "Slim"})
        (harness.repo / "note.txt").write_text("hello\n", encoding="utf-8")
        read_job = await mcp_call(client, "read_file", {"task_id": task["id"], "path": "note.txt"})
        assert "continuation" not in read_job
        assert "next_step" not in read_job

        snap = await mcp_call(client, "task_get", {"task_id": task["id"]})
        assert "continuation" in snap
        assert "deliveries" not in snap["continuation"]

        presented = await mcp_call(client, "task_present", {"task_id": task["id"]})
        assert "deliveries" in presented["continuation"]


async def test_read_process_inline_wait_finishes_in_single_call(harness):
    async with harness.mcp() as client:
        task = await mcp_call(client, "task_create", {"project": "local/project", "title": "Wait"})
        started = await mcp_call(
            client,
            "exec_command",
            {
                "task_id": task["id"],
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.6); print('delayed-output')",
                ],
                "yield_time_ms": 0,
                "request_id": "start-delayed",
            },
        )
        if started["status"] != "succeeded":
            started = await mcp_call(
                client, "job_get", {"job_id": started["job_id"], "wait_seconds": 5}
            )
        pid = started["result"]["process_id"]
        cursor = started["result"]["next_cursor"]
        observation = await client.call_tool(
            "read_process",
            {
                "task_id": task["id"],
                "process_id": pid,
                "cursor": cursor,
                "wait_seconds": 3,
                "request_id": "wait-inline",
            },
        )
        assert not observation.isError
        observed = observation.structuredContent
        assert observed["status"] == "succeeded"
        assert observed["result"]["status"] == "completed"
        assert "delayed-output" in observed["result"]["output"]
        assert "next_action" not in observed


async def test_cli_read_process_can_wait_for_30_seconds(harness, tmp_path):
    profile = tmp_path / "cli.json"
    profile.write_text(json.dumps({"server_url": harness.url, "token": "c" * 40}), encoding="utf-8")
    async with harness.mcp() as client:
        task = await mcp_call(
            client, "task_create", {"project": "local/project", "title": "CLI wait"}
        )
        started = await mcp_call(
            client,
            "exec_command",
            {
                "task_id": task["id"],
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.readline(); print('released')",
                ],
                "yield_time_ms": 0,
            },
        )
        process = {"task_id": task["id"], "process_id": started["result"]["process_id"]}
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "webcodex",
            "call",
            "read_process",
            "--config",
            str(profile),
            "--arguments",
            json.dumps({**process, "wait_seconds": 30}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(cli.communicate(), 50)
            assert cli.returncode == 0, stderr.decode(errors="replace")
            observation = json.loads(stdout)
            assert observation["status"] == "succeeded"
            assert observation["result"]["status"] == "running"
        finally:
            if cli.returncode is None:
                cli.kill()
                await cli.wait()
            await mcp_call(client, "write_stdin", {**process, "chars": "\n"})
        finished = await mcp_call(client, "read_process", {**process, "wait_seconds": 5})
        assert finished["result"]["status"] == "completed"
        assert "released" in finished["result"]["output"]


@pytest.mark.parametrize("existing_observation", [False, True])
async def test_refresh_task_wait_forwards_cursors_or_adopts_existing_job(
    tmp_path, existing_observation
):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    try:
        service.store.register("local", "instance", {"project": "b" * 64})
        task = service.store.create_task("owner", "local/project", "Watch")
        job = service.store.enqueue(task, "exec_command", {}, "run-1", 20)
        service.store.claim("local", "instance", 1)
        service.store.complete(
            "local",
            job["job_id"],
            "instance",
            "succeeded",
            {"process_id": "proc-1", "status": "running", "next_cursor": 123},
        )
        wait = service.continuations.arm(task["id"], [job["job_id"]], "watch-req")
        observation_id = "obs-1"
        if existing_observation:
            legacy = service.store.enqueue(
                task,
                "read_process",
                {"process_id": "proc-1", "cursor": 0, "max_bytes": 65536, "wait_seconds": 0},
                f"watch:{wait['id']}:{job['job_id']}",
                20,
            )
            observation_id = legacy["job_id"]
            service.store.claim("local", "instance", 1)
            service.store.complete(
                "local",
                observation_id,
                "instance",
                "succeeded",
                {"process_id": "proc-1", "status": "completed", "exit_code": 0},
            )
        calls = []

        async def fake_call(owner, name, arguments):
            calls.append((owner, name, arguments))
            return {"job_id": "obs-1", "status": "succeeded", "result": {"status": "completed"}}

        service.call = fake_call
        await service.refresh_task_wait(task)
        updated = service.continuations.current(task["id"])
        assert updated["state"] == "ready"
        assert json.loads(updated["jobs"]) == [observation_id]
        if existing_observation:
            assert calls == []
        else:
            [(_, name, args)] = calls
            assert name == "read_process"
            assert args["cursor"] == 123
            assert args["max_bytes"] == 8192
            assert args["request_id"].startswith("watch:")
    finally:
        service.store.close()


async def test_task_handoff_sql_extracts_paths_without_reading_arguments_blobs(tmp_path):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    try:
        service.store.register("local", "instance", {"project": "b" * 64})
        task = service.store.create_task("owner", "local/project", "Handoff paths")
        for req, tool, args in (
            ("w1", "write_file", {"path": "a.py", "content": "X" * 50000}),
            ("e1", "edit_file", {"path": "b.py", "target": "x", "replacement": "y"}),
            (
                "p1",
                "apply_patch",
                {"patch": "--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-x\n+y\n"},
            ),
        ):
            j = service.store.enqueue(task, tool, args, req, 20)
            service.store.claim("local", "instance", 1)
            service.store.complete("local", j["job_id"], "instance", "succeeded", {"ok": True})

        queries = []
        service.store.db.set_trace_callback(queries.append)
        try:
            progress = service.task_progress(task, None)
            handoff = service.task_handoff(task, progress)
        finally:
            service.store.db.set_trace_callback(None)
        assert handoff["changed_files"] == ["a.py", "b.py", "c.py"]
        assert any("json_extract(arguments, '$.path')" in q for q in queries)
        assert not any(
            "SELECT arguments FROM jobs INDEXED BY task_job_history" in q for q in queries
        )
    finally:
        service.store.close()


async def test_search_files_splits_long_glob_lists_over_16k(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sub = root / ("subdir_" + "a" * 160)
    sub.mkdir()
    for index in range(120):
        (sub / f"file_{index:03d}_long_name.txt").write_text(f"needle {index}\n", encoding="utf-8")

    cfg = RunnerConfig(
        server_url="http://127.0.0.1:18765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "runner",
        projects={"p": root},
    )
    processes = Processes(cfg)
    workspace = Workspace(root)
    runtime = Runtime(cfg, workspace, processes, Extensions(cfg, "p"), "t1", "j1", Computer())
    try:
        pages = 0
        outputs = []
        offset: int | None = 0
        while offset is not None:
            pages += 1
            runtime.job_id = f"j{pages}"
            page = await runtime.search_files(query="needle", file_offset=offset)
            next_offset = page["next_file_offset"]
            if pages == 1:
                assert next_offset is not None and 0 < next_offset < 120
            outputs.append(page["output"])
            while page["status"] == "running" or page["has_more"]:
                if page["status"] == "running":
                    await processes.wait(page["process_id"], "t1", 2)
                page = processes.read(page["process_id"], "t1", cursor=page["next_cursor"])
                outputs.append(page["output"])
            offset = next_offset
        assert pages >= 2
        combined = "".join(outputs)
        assert "file_000_long_name.txt" in combined
        assert "file_119_long_name.txt" in combined
    finally:
        processes.db.close()


async def test_job_get_wakes_immediately_when_archived_mid_wait(tmp_path):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    try:
        service.store.register("local", "instance", {"project": "b" * 64})
        task = service.store.create_task("owner", "local/project", "Archive wakeup")
        job = service.store.enqueue(task, "workspace_info", {}, "q1", 20)
        waiter = asyncio.create_task(
            service.call("owner", "job_get", {"job_id": job["job_id"], "wait_seconds": 10})
        )
        await asyncio.sleep(0.05)
        started = time.monotonic()
        service.set_task_archive("owner", task["id"], time.time())
        result = await asyncio.wait_for(waiter, 1.5)
        assert time.monotonic() - started < 1.0
        assert "archived" in result["next_step"].lower()
        snapshot = await service.call("owner", "task_get", {"task_id": task["id"]})
        assert snapshot["status"] == "active"
        assert snapshot["progress"]["state"] == "archived"
        assert snapshot["progress"]["next_actions"] == []
        assert service.store.claim("local", "instance", 1) == []
        with pytest.raises(TaskArchived):
            await service.call("owner", "task_takeover", {"task_id": task["id"]})
    finally:
        service.store.close()


def test_store_enqueue_request_id_idempotent_with_begin_immediate(tmp_path):
    service = Service(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    try:
        service.store.register("local", "instance", {"project": "b" * 64})
        task = service.store.create_task("owner", "local/project", "Idempotent")
        first = service.store.enqueue(task, "read_file", {"path": "a.py"}, "same-req", 10)
        second = service.store.enqueue(task, "read_file", {"path": "a.py"}, "same-req", 10)
        assert first["job_id"] == second["job_id"]
        with pytest.raises(ValueError, match="different call"):
            service.store.enqueue(task, "read_file", {"path": "b.py"}, "same-req", 10)
        assert not service.store.db.in_transaction
    finally:
        service.store.close()


async def test_retry_matches_legacy_defaults_only_when_omitted(harness):
    harness.config.clients["owner"].permissions.add("computer_observe")
    service = harness.app.state.service
    task = service.store.create_task("owner", "local/project", "Retry earlier jobs")
    async with harness.http() as client:
        for tool, supplied, legacy_defaults, explicit in (
            (
                "exec_command",
                {"argv": [sys.executable, "-V"]},
                {"yield_time_ms": 500, "timeout_seconds": 3600},
                {"yield_time_ms": 2000},
            ),
            ("computer_screenshot", {}, {"max_dimension": 1600}, {"max_dimension": 1280}),
        ):
            request_id = f"legacy-{tool}"
            arguments = {"task_id": task["id"], "request_id": request_id, **supplied}
            old_arguments = (
                REMOTE[tool]
                .model.model_validate(arguments)
                .model_dump(exclude={"task_id", "request_id"})
            )
            old = service.store.enqueue(
                task, tool, {**old_arguments, **legacy_defaults}, request_id, 20
            )
            service.store.claim("local", harness.runner.instance, 1)
            service.store.complete("local", old["job_id"], harness.runner.instance, "succeeded", {})

            response = await client.post("/api/call", json={"name": tool, "arguments": arguments})
            assert response.status_code == 200
            assert response.json()["job_id"] == old["job_id"]
            changed = await client.post(
                "/api/call", json={"name": tool, "arguments": {**arguments, **explicit}}
            )
            assert changed.status_code == 400
            assert "different call" in changed.json()["error"]


async def test_runner_poll_limits_yield_for_legacy_runner_only(tmp_path):
    app = create_app(
        ServerConfig(
            state_dir=tmp_path / "state",
            admin_token="a" * 40,
            clients={"owner": Client(token="c" * 40, projects=["local/project"])},
            runners={"local": "r" * 40},
        )
    )
    service = app.state.service
    store = service.store
    try:
        store.register("local", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "local/project", "Mixed Runner versions")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "r" * 40},
        ) as client:
            for capability, expected in (({}, 2000), ({"supports_long_exec_yield": True}, 3000)):
                job = store.enqueue(
                    task, "exec_command", {"argv": ["example"], "yield_time_ms": 3000}, None, 20
                )
                response = await client.post(
                    "/api/runner/poll", json={"instance": "instance", **capability}
                )
                response.raise_for_status()
                [claimed] = response.json()["jobs"]
                assert claimed["id"] == job["job_id"]
                assert claimed["arguments"]["yield_time_ms"] == expected
                saved = store.one("SELECT arguments FROM jobs WHERE id=?", (job["job_id"],))
                assert saved["arguments"]["yield_time_ms"] == 3000
    finally:
        await app.state.management_sources.close()
        store.close()
        service.diagnostics.close()


async def test_task_get_keeps_unanswered_questions_visible(harness):
    store = harness.app.state.service.store
    task = store.create_task("owner", "local/project", "Operator question")
    async with harness.http() as client, harness.http(token="a" * 40) as admin:

        async def ask(question):
            response = await client.post(
                "/api/call",
                json={
                    "name": "request_user_input",
                    "arguments": {"task_id": task["id"], "question": question},
                },
            )
            response.raise_for_status()
            return response.json()["question_id"]

        unanswered = await ask("Still waiting")
        for index in range(10):
            answered = await ask(f"Answered question {index}")
            response = await admin.post(f"/api/admin/questions/{answered}", json={"answer": "Done"})
            response.raise_for_status()
        response = await client.post(
            "/api/call", json={"name": "task_get", "arguments": {"task_id": task["id"]}}
        )
        response.raise_for_status()
        snapshot = response.json()
        assert snapshot["progress"]["state"] == "waiting_operator"
        assert snapshot["questions"][0] == {
            "id": unanswered,
            "question": "Still waiting",
            "answer": None,
        }
        assert len(snapshot["questions"]) == 10


async def test_security_reraises_after_response_start(tmp_path):
    cfg = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={"owner": Client(token="c" * 40, projects=["local/project"])},
        runners={"local": "r" * 40},
    )
    auth = ManagementAuth(cfg)

    async def app_before_start(scope, receive, send):
        raise ValueError("bad input before start")

    async def app_after_start(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("mid-stream failure")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/call",
        "headers": [(b"authorization", b"Bearer " + b"c" * 40)],
    }

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent_before = []

    async def send_before(msg):
        sent_before.append(msg)

    await Security(app_before_start, cfg, auth)(dict(scope), receive, send_before)
    assert any(m.get("status") == 400 for m in sent_before if m["type"] == "http.response.start")

    sent_after = []

    async def send_after(msg):
        sent_after.append(msg)

    with pytest.raises(ValueError, match="mid-stream failure"):
        await Security(app_after_start, cfg, auth)(dict(scope), receive, send_after)
    assert len([m for m in sent_after if m["type"] == "http.response.start"]) == 1


@pytest.mark.parametrize(
    ("tool", "result"),
    [
        ("read_file", {"path": "retention", "content": "A" * 4096}),
        (
            "plugin_call",
            {
                "mcp_result": {
                    "content": [{"type": "image", "mimeType": "image/png", "data": "A" * 4096}],
                    "structuredContent": {"retention": "test value"},
                }
            },
        ),
    ],
)
def test_retention_cleans_archived_tasks(tmp_path, tool, result):
    repo = tmp_path / "repo"
    repo.mkdir()
    directory = tmp_path / "private"
    initialize(directory, repo, 19877)
    proc_dir = directory / "runner" / "processes"
    proc_dir.mkdir(parents=True)
    with sqlite3.connect(proc_dir / "processes.sqlite3") as db:
        db.execute(
            "CREATE TABLE processes(id TEXT PRIMARY KEY, task_id TEXT, status TEXT, created REAL, finished REAL)"
        )
    with sqlite3.connect(directory / "runner" / "deliveries.sqlite3") as db:
        db.execute(
            "CREATE TABLE deliveries(id TEXT PRIMARY KEY,status TEXT,result TEXT,acknowledged INTEGER)"
        )
    store = Service(ServerConfig.model_validate_json((directory / "server.json").read_text())).store
    try:
        store.register("local", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "local/project", "Archived task")
        job = store.enqueue(task, tool, {}, "stored-result", 20)
        store.claim("local", "instance", 1)
        store.complete(
            "local",
            job["job_id"],
            "instance",
            "succeeded",
            result,
        )
        old = time.time() - 40 * 86400
        with store.db:
            store.db.execute(
                "UPDATE tasks SET archived_at=?, updated=? WHERE id=?", (old, old, task["id"])
            )
            store.db.execute("UPDATE jobs SET updated=? WHERE id=?", (old, job["job_id"]))
        res = retention(directory, days=30, apply=True)
        assert res["selected_bytes"] > 0
        assert "payload_removed_at" in store.job(job["job_id"], "owner")["result"]["retention"]
        assert retention(directory, days=30, apply=True)["selected_bytes"] == 0
        assert store.task(task["id"], "owner")["status"] == "active"
    finally:
        store.close()


async def test_management_sources_preserves_last_seen_at(tmp_path, monkeypatch):
    cfg = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={
            "owner": Client(
                token="c" * 40,
                projects=["local/project"],
                management_sources={
                    "win": ManagementSource(url="https://remote.example", token="t" * 40, label="W")
                },
            )
        },
        runners={"local": "r" * 40},
    )
    service = Service(cfg)
    sources = Sources(Management(service))
    try:

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return httpx.Response(
                    200,
                    json={
                        "incidents": [
                            {
                                "id": "inc-1",
                                "source_id": "local",
                                "component": "runner",
                                "reason": "source_unreachable",
                                "started_at": 100.0,
                                "last_seen_at": 250.0,
                                "recovered_at": None,
                            }
                        ],
                        "runners": [],
                    },
                )

        monkeypatch.setattr(sources, "remote_client", lambda owner, source_id: FakeClient())
        out = await sources.remote_connections("owner", "win")
        assert len(out["incidents"]) == 1
        assert out["incidents"][0]["started_at"] == 100.0
        assert out["incidents"][0]["last_seen_at"] == 250.0
    finally:
        service.store.close()
