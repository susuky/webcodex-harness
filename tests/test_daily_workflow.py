import json
import subprocess
import sys

import pytest
from test_harness import Harness, call

from webcodex.config import (
    DEFAULT_PERMISSIONS,
    Client,
    RunnerConfig,
    ServerConfig,
    allow_project_root,
    initialize,
    load_config,
)
from webcodex.server import Service, TaskTools

pytest_plugins = ["test_harness"]


async def review(client, task_id, path=None):
    value = await call(client, "task_review", {"task_id": task_id, "path": path})
    for _ in range(20):
        if value["review"] is not None:
            return value["review"]
        assert value["review_job"]["status"] in {"queued", "running"}, value
        value = await call(
            client,
            "task_review",
            {
                "task_id": task_id,
                "path": path,
                "job_id": value["review_job"]["job_id"],
            },
        )
    pytest.fail("Review did not finish")


async def test_each_user_turn_can_present_the_same_task_again(harness):
    async with harness.mcp() as client:
        specs = (await client.list_tools()).tools
        assert {spec.name for spec in specs if spec.meta.get("ui", {}).get("resourceUri")} == {
            "task_present"
        }
        selection = await call(client, "work_on_project")
        assert selection["selection_required"] and "next_action" not in selection

        opened = await call(
            client,
            "work_on_project",
            {"project": "local/project", "title": "Repeat presentation", "request_id": "start"},
        )
        task_id = opened["task"]["id"]
        for turn in range(2):
            if turn:
                opened = await call(client, "work_on_project", {"task_id": task_id})
            action = opened["next_action"]
            assert action == {"tool": "task_present", "arguments": {"task_id": task_id}}
            presented = await call(client, action["tool"], action["arguments"])
            assert presented["task"]["id"] == task_id
            assert presented["task"]["status"] == "active"
        assert [task["id"] for task in (await call(client, "task_list"))["tasks"]] == [task_id]


async def test_new_project_inherits_trust_and_completed_work_remains_reviewable(tmp_path):
    harness = Harness(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    harness.runner_config.project_roots = {"work": root}
    harness.runner_config.permissions.add("projects")
    owner = harness.config.clients["owner"]
    owner.project_roots = ["local/work"]
    owner.trusted_project_roots = ["local/work"]
    owner.permissions.add("projects")
    owner.approval_required = {"projects", "execute", "git_write", "extensions"}
    await harness.start()
    try:
        async with harness.mcp() as client, harness.mcp("d" * 40) as other:
            roots = (await call(client, "project_roots_list"))["roots"]
            assert roots[0]["approval_mode"] == roots[0]["new_project_approval_mode"] == "automatic"
            creation = await call(
                client,
                "project_create",
                {
                    "root": "local/work",
                    "name": "my-app",
                    "git_init": True,
                    "request_id": "new",
                },
            )
            assert creation["status"] == "succeeded"
            start = creation["next_action"]
            opened = await call(client, start["tool"], start["arguments"])
            task = opened["task"]["id"]
            assert opened["task"]["progress"]["approval_mode"] == "automatic"
            assert (await call(client, start["tool"], start["arguments"]))["task"]["id"] == task
            assert opened["workspace"]["status"] in {"queued", "running", "succeeded"}
            await call(
                client,
                "write_file",
                {"task_id": task, "path": "app.py", "content": "print('hello')\n"},
            )
            command = await call(
                client,
                "exec_command",
                {
                    "task_id": task,
                    "argv": [sys.executable, "-c", "print('check failed'); raise SystemExit(3)"],
                    "request_id": "test-once",
                    "yield_time_ms": 2000,
                },
            )
            assert command["status"] == "succeeded" and command["result"]["exit_code"] == 3
            await call(
                client,
                "task_update",
                {
                    "task_id": task,
                    "status": "completed",
                    "summary": "已保存，測試失敗待確認",
                    "plan": [],
                },
            )
            inspected = await review(client, task)
            assert inspected["files"][0]["path"] == "app.py"
            assert inspected["files"][0]["status"] == "untracked"
            assert inspected["executions"][0]["exit_code"] == 3
            assert "check failed" in inspected["executions"][0]["output"]
            preview = await review(client, task, "app.py")
            assert preview["preview"]["text"] == "print('hello')\n"
            assert (await call(client, "read_file", {"task_id": task, "path": "app.py"}))[
                "status"
            ] == "succeeded"
            await call(
                client,
                "write_file",
                {"task_id": task, "path": "blocked.txt", "content": "no"},
                error=True,
            )
            await call(other, "task_review", {"task_id": task}, error=True)
            resumed = await call(client, "work_on_project", {"task_id": task})
            assert resumed["task"]["status"] == "completed"
            assert (
                len(
                    [
                        r
                        for r in harness.app.state.service.store.rows(
                            "SELECT tool FROM jobs WHERE task_id=?", (task,)
                        )
                        if r["tool"] == "exec_command"
                    ]
                )
                == 1
            )
    finally:
        await harness.close()


def test_root_setup_preserves_credentials_and_scopes_trust(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    repo = root / "existing"
    repo.mkdir()
    config_dir = tmp_path / "private"
    initialize(config_dir, repo, 9876, False)
    before = {
        name: (config_dir / name).read_bytes()
        for name in ("server.json", "runner.json", "client.json")
    }
    original = json.loads(before["server.json"])
    configured = allow_project_root(config_dir, root, trusted=True)
    server = load_config(config_dir / "server.json", ServerConfig)
    runner = load_config(config_dir / "runner.json", RunnerConfig)
    policy = server.clients["owner"]
    root_id = configured["project_root"]
    assert root_id in policy.project_roots and root_id in policy.trusted_project_roots
    assert server.admin_token == original["admin_token"]
    assert policy.token == original["clients"]["owner"]["token"]
    assert (config_dir / "client.json").read_bytes() == before["client.json"]
    assert DEFAULT_PERMISSIONS | {"projects"} <= runner.permissions
    assert "projects" not in policy.approvals_for(root_id, "project")
    assert "execute" not in policy.approvals_for("local/future", project_root=root_id)
    assert "execute" in policy.approvals_for("local/outside")
    assert "computer_control" in policy.approvals_for("local/future", project_root=root_id)
    policy.project_roots = []
    assert "projects" in policy.approvals_for(root_id, "project")
    assert "execute" in policy.approvals_for("local/future", project_root=root_id)
    with pytest.raises(ValueError, match="settings"):
        allow_project_root(config_dir, tmp_path, trusted=True)


async def test_unavailable_project_does_not_disconnect_other_projects(tmp_path):
    harness = Harness(tmp_path)
    missing = tmp_path / "removed"
    missing.mkdir()
    harness.runner_config.projects["removed"] = missing
    harness.runner_config.project_roots = {"removed-root": missing}
    harness.config.clients["owner"].projects.append("local/removed")
    await harness.start()
    try:
        missing.rmdir()
        async with harness.http("r" * 40) as connection:
            await harness.runner.register(connection, refresh_inventory=True)
        async with harness.mcp() as client:
            projects = (await call(client, "projects_list"))["projects"]
            assert [item["id"] for item in projects] == ["local/project"]
            task = (
                await call(
                    client, "task_create", {"project": "local/project", "title": "still usable"}
                )
            )["id"]
            await call(client, "write_file", {"task_id": task, "path": "ok.txt", "content": "ok"})
            view = await review(client, task, "ok.txt")
            assert not view["is_git_repository"] and view["preview"]["text"] == "ok"
        # Retained missing paths must not prevent an installation from starting next time.
        RunnerConfig.model_validate(harness.runner_config.model_dump())
    finally:
        await harness.close()


def test_root_trust_releases_only_authorized_original_pending_jobs(tmp_path):
    owner = Client(
        token="c" * 40,
        projects=["local/outside"],
        project_roots=["local/work"],
        permissions=DEFAULT_PERMISSIONS | {"projects"},
    )
    config = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={"owner": owner},
        runners={"local": "r" * 40},
    )
    service = Service(config)
    service.store.register(
        "local",
        "one",
        {"new": "n" * 64, "outside": "o" * 64},
        project_roots={"work": {"binding": "w" * 64, "projects": ["new"]}},
    )
    task = service.store.create_task("owner", "local/new", "new")
    outside = service.store.create_task("owner", "local/outside", "outside")
    jobs = [
        service.store.enqueue(t, "exec_command", {"argv": ["echo", "ready"]}, "once", True, 10)
        for t in (task, outside)
    ]
    owner.trusted_project_roots = ["local/work"]
    service.release_pending_approvals()
    assert service.store.job(jobs[0]["job_id"], "owner")["status"] == "queued"
    assert service.store.job(jobs[1]["job_id"], "owner")["status"] == "pending_approval"
    owner.project_roots = []
    assert service.claim("local", "one", 10) == []
    assert service.store.job(jobs[0]["job_id"], "owner")["status"] == "failed"
    service.store.close()


async def test_review_tracks_staged_unstaged_renames_deletions_and_binary(harness):
    def git(*args):
        return subprocess.run(["git", *args], cwd=harness.repo, check=True, capture_output=True)

    git("init", "--initial-branch=main")
    for name in ("mixed.txt", "deleted.txt", "before.txt", "[one].txt"):
        (harness.repo / name).write_text("original\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
    (harness.repo / "mixed.txt").write_text("staged\n")
    git("add", "mixed.txt")
    (harness.repo / "mixed.txt").write_text("unstaged\n")
    (harness.repo / "deleted.txt").unlink()
    git("mv", "before.txt", "after.txt")
    (harness.repo / "[one].txt").write_text("literal path\n")
    (harness.repo / "photo.bin").write_bytes(b"\x00\x01")
    async with harness.mcp() as client:
        task = (await call(client, "task_create", {"project": "local/project", "title": "Review"}))[
            "id"
        ]
        result = await review(client, task)
        files = {row["path"]: row for row in result["files"]}
        assert files["after.txt"]["old_path"] == "before.txt"
        assert files["deleted.txt"]["status"] == "deleted"
        mixed = (await review(client, task, "mixed.txt"))["preview"]["diffs"]
        assert "+unstaged" in mixed[0]["text"] and not mixed[0]["staged"]
        assert "+staged" in mixed[1]["text"] and mixed[1]["staged"]
        literal = await review(client, task, "[one].txt")
        assert "+literal path" in literal["preview"]["diffs"][0]["text"]
        assert (await review(client, task, "photo.bin"))["preview"]["kind"] == "binary"
        blocked = await call(client, "task_review", {"task_id": task, "path": "../secret"})
        job = await call(client, "job_get", {"job_id": blocked["review_job"]["job_id"]}, error=True)
        assert job["status"] == "failed"


async def test_task_list_filters_before_pagination(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    service = Service(harness.config)
    try:
        service.store.register("local", "one", {"project": "a" * 64, "hidden": "b" * 64})
        monkeypatch.setattr("webcodex.store.time.time", lambda: 1000.0)
        first = service.store.create_task("owner", "local/project", "first")
        second = service.store.create_task("owner", "local/project", "second")
        service.store.create_task("owner", "local/hidden", "hidden")
        task_tools = TaskTools(service, "owner")
        page = await task_tools.task_list(limit=1)
        assert [t["id"] for t in page["tasks"]] == [second["id"]]
        page = await task_tools.task_list(limit=1, offset=page["next_offset"])
        assert [t["id"] for t in page["tasks"]] == [first["id"]]
        assert page["next_offset"] is None
    finally:
        service.store.close()
        harness.socket.close()
