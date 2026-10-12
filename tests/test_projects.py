import asyncio
import os
import sqlite3

import httpx
import pytest
from test_harness import Harness, call

from webcodex.config import MAX_PROJECTS, RunnerConfig, project_binding
from webcodex.projects import ProjectOperations, restore_projects
from webcodex.runner import Runner
from webcodex.server import Service
from webcodex.store import Store


@pytest.fixture
def project_registry(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={},
        project_roots={"work": root},
    )
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    restore_projects(config, db)
    try:
        yield config, db, root
    finally:
        db.close()


def registered_project(db, root, name):
    path = root / name
    path.mkdir()
    with db:
        db.execute(
            "INSERT INTO managed_projects VALUES (?,?,?,?,?)",
            (name, "work", name, project_binding(path), project_binding(root)),
        )
    return path


def test_restore_project_limit_rejects_without_partial_configuration(project_registry):
    config, db, root = project_registry
    configured = {}
    for index in range(MAX_PROJECTS - 1):
        path = root / f"configured-{index}"
        path.mkdir()
        configured[f"configured-{index}"] = path
    config = RunnerConfig.model_validate({**config.model_dump(), "projects": configured})
    registered_project(db, root, "managed-one")
    registered_project(db, root, "managed-two")

    with pytest.raises(ValueError, match=r"total 101.*runner.json.*registrations are retained"):
        restore_projects(config, db)

    assert config.projects == configured
    assert db.execute("SELECT count(*) FROM managed_projects").fetchone()[0] == 2
    assert (root / "managed-one").is_dir() and (root / "managed-two").is_dir()


async def test_restore_exact_project_limit_blocks_additional_registration(project_registry):
    config, db, root = project_registry
    for index in range(MAX_PROJECTS - 1):
        path = root / f"configured-{index}"
        path.mkdir()
        config.projects[f"configured-{index}"] = path
    registered_project(db, root, "managed")
    missing = registered_project(db, root, "missing")
    missing.rmdir()

    restore_projects(config, db)

    assert len(config.inventory()) == MAX_PROJECTS
    assert "managed" in config.projects and "missing" not in config.projects
    operations = ProjectOperations(config, db, None, "management")
    with pytest.raises(ValueError, match=f"{MAX_PROJECTS}-project limit"):
        await operations.project_create("local/work", "extra", "limit")
    assert not (root / "extra").exists()
    assert db.execute("SELECT count(*) FROM managed_projects").fetchone()[0] == 2


async def test_create_and_register_projects_from_empty_inventory(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    root = tmp_path / "新專案 roots"
    root.mkdir()
    harness.runner_config.projects = {}
    harness.runner_config.project_roots = {"work": root}
    harness.runner_config.permissions.add("projects")
    owner = harness.config.clients["owner"]
    owner.projects = []
    owner.project_roots = ["local/work"]
    owner.permissions.add("projects")
    owner.approval_required = {"projects"}
    await harness.start()
    try:
        async with harness.mcp() as client, harness.mcp("d" * 40) as other:
            assert (await call(client, "projects_list"))["projects"] == []
            roots = (await call(client, "project_roots_list"))["roots"]
            assert roots[0]["id"] == "local/work" and roots[0]["online"] is True
            assert (await call(other, "project_roots_list"))["roots"] == []
            specs = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert "task_id" not in specs["project_create"].inputSchema["properties"]
            assert specs["project_roots_list"].annotations.readOnlyHint
            assert not specs["project_create"].annotations.readOnlyHint
            args = {
                "root": "local/work",
                "name": "brand-new",
                "git_init": True,
                "request_id": "new-1",
            }
            job = await call(client, "project_create", args)
            assert job["status"] == "pending_approval"
            assert not (root / "brand-new").exists()
            assert (await call(client, "project_create", args))["job_id"] == job["job_id"]
            await call(client, "project_create", {**args, "name": "different"}, error=True)
            register = harness.runner.register
            lost_reply = False

            async def lose_registration_reply_once(connection, **options):
                nonlocal lost_reply
                await register(connection, **options)
                if "brand-new" in harness.runner.config.projects and not lost_reply:
                    lost_reply = True
                    raise httpx.ReadError("Simulated lost inventory acknowledgement")

            monkeypatch.setattr(harness.runner, "register", lose_registration_reply_once)
            async with harness.http("a" * 40) as admin:
                assert (
                    await admin.post("/api/admin/approvals/" + job["job_id"], json={"allow": True})
                ).status_code == 200
            done = await call(client, "job_get", {"job_id": job["job_id"], "wait_seconds": 20})
            assert done["result"]["project"] == "local/brand-new"
            assert lost_reply
            assert (root / "brand-new/.git").is_dir()
            assert (await call(client, "projects_list"))["projects"][0]["id"] == "local/brand-new"
            assert (await call(other, "projects_list"))["projects"] == []
            await call(
                other, "task_create", {"project": "local/brand-new", "title": "denied"}, error=True
            )
            await call(client, "workspace_info", {"task_id": job["task_id"]}, error=True)
            await call(
                client,
                "task_update",
                {"task_id": job["task_id"], "status": "completed", "summary": "", "plan": []},
                error=True,
            )
            task = (
                await call(
                    client, "task_create", {"project": "local/brand-new", "title": "開始新專案"}
                )
            )["id"]
            await call(
                client, "write_file", {"task_id": task, "path": "hello.txt", "content": "新專案\n"}
            )
            assert (
                "hello.txt"
                in (await call(client, "git_status", {"task_id": task}))["result"]["status"]
            )

            owner.approval_required = set()
            existing = root / "existing"
            existing.mkdir()
            (existing / "keep.txt").write_text("keep", encoding="utf-8")
            await call(
                client,
                "project_create",
                {"root": "local/work", "name": "existing", "request_id": "exists"},
                error=True,
            )
            imported = await call(
                client,
                "project_register",
                {
                    "root": "local/work",
                    "name": "imported",
                    "path": "existing",
                    "request_id": "import",
                },
            )
            assert (
                imported["result"]["registered"] and (existing / "keep.txt").read_text() == "keep"
            )
            await call(
                client,
                "project_register",
                {"root": "local/work", "name": "alias", "path": "existing", "request_id": "alias"},
                error=True,
            )
            for index, path in enumerate(("../outside", str(tmp_path), ".")):
                await call(
                    client,
                    "project_register",
                    {
                        "root": "local/work",
                        "name": "escape",
                        "path": path,
                        "request_id": f"bad-{index}",
                    },
                    error=True,
                )
            if os.name != "nt":
                (root / "linked").symlink_to(tmp_path, target_is_directory=True)
                await call(
                    client,
                    "project_register",
                    {"root": "local/work", "name": "link", "path": "linked", "request_id": "link"},
                    error=True,
                )
            results = await asyncio.gather(
                *(
                    call(
                        client,
                        "project_create",
                        {"root": "local/work", "name": name, "request_id": name},
                    )
                    for name in ("parallel-one", "parallel-two")
                )
            )
            assert all(r["status"] == "succeeded" for r in results)
            for name in ("parallel-one", "parallel-two"):
                assert list((root / name).iterdir()) == []

            harness.runner.stop.set()
            await asyncio.wait_for(harness.runner_task, 10)
            with harness.app.state.service.store.db as db:
                db.execute("UPDATE runners SET seen=0")
            harness.runner = Runner(harness.runner_config)
            assert "brand-new" in harness.runner.config.projects
            harness.runner_task = asyncio.create_task(harness.runner.run())
            # A coding job resumes after the restored Runner registers its persistent inventory.
            read = await call(client, "read_file", {"task_id": task, "path": "hello.txt"})
            assert "新專案" in read["result"]["content"]
            assert (await call(client, "project_create", args))["job_id"] == job["job_id"]
            assert not (root / "brand-new/.git/logs/HEAD").exists()
    finally:
        await harness.close()


async def test_project_creation_authorization_is_rechecked_before_dispatch(tmp_path):
    harness = Harness(tmp_path)
    policy = harness.config.clients["owner"]
    policy.permissions.add("projects")
    policy.project_roots = ["local/work"]
    policy.approval_required = {"projects"}
    service = Service(harness.config)
    service.store.register(
        "local", "one", {}, project_roots={"work": {"binding": "a" * 64, "projects": []}}
    )
    try:
        args = {"root": "local/work", "name": "new", "request_id": "one"}
        with pytest.raises(ValueError, match="authorized"):
            await service.call("owner", "project_create", {**args, "root": "local/other"})
        job = await service.call("owner", "project_create", args)
        service.store.approve(job["job_id"], True)
        policy.project_roots = []
        assert service.claim("local", "one", 10) == []
        assert service.store.job(job["job_id"], "owner")["status"] == "failed"
    finally:
        service.store.close()
        harness.socket.close()


def test_legacy_database_migration_preserves_tasks(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "one", {"project": "a" * 64})
    task = store.create_task("owner", "runner/project", "existing")
    store.close()
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.execute("ALTER TABLE tasks DROP COLUMN kind")
        db.execute("ALTER TABLE runners DROP COLUMN project_roots")
    store = Store(tmp_path)
    try:
        assert store.task(task["id"], "owner")["kind"] == "coding"
        assert store.inventory()[0]["id"] == "runner/project"
        assert store.inventory(roots=True) == []
    finally:
        store.close()


async def test_replaced_parent_root_cannot_receive_approved_creation(tmp_path):
    harness = Harness(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    harness.runner_config.project_roots = {"work": root}
    harness.runner_config.permissions.add("projects")
    runner = Runner(harness.runner_config)
    original = project_binding(root)
    root.rename(tmp_path / "original-projects")
    root.mkdir()
    job = {
        "id": "create-after-replacement",
        "instance": runner.instance,
        "task_id": "management",
        "kind": "project",
        "tool": "project_create",
        "project": "work",
        "binding": original,
        "arguments": {"root": "local/work", "name": "new", "request_id": "create"},
    }
    try:
        await runner.execute(job)
        result = runner.db.execute("SELECT status,result FROM deliveries").fetchone()
        assert result["status"] == "failed" and "binding" in result["result"]
        assert not (root / "new").exists()
        assert not (tmp_path / "original-projects/new").exists()
    finally:
        await runner.processes.close()
        runner.db.close()
        runner.lock_file.close()
        harness.socket.close()
