import sys

import pytest
from test_harness import Harness, call, process_result

from webcodex.config import Client, ServerConfig
from webcodex.server import Service


async def test_trusted_project_executes_without_approval_and_keeps_permissions(tmp_path):
    harness = Harness(tmp_path)
    owner = harness.config.clients["owner"]
    owner.approval_required = {"execute", "git_write", "extensions", "computer_control", "projects"}
    owner.permissions.add("projects")
    owner.trusted_projects = ["local/project"]
    await harness.start()
    try:
        async with harness.mcp() as client, harness.mcp("d" * 40) as reviewer:
            projects = await call(client, "projects_list")
            assert projects["projects"][0]["approval_mode"] == "automatic"
            assert projects["projects"][0]["approval_required"] == []
            assert (await call(reviewer, "projects_list"))["projects"][0][
                "approval_mode"
            ] == "review"
            task = (
                await call(client, "task_create", {"project": "local/project", "title": "trusted"})
            )["id"]
            args = {
                "task_id": task,
                "argv": [sys.executable, "-c", "print('ready')"],
                "request_id": "once",
            }
            job = await call(client, "exec_command", args)
            assert job["status"] == "succeeded" and job["approved"] == 0
            result = await process_result(client, task, job)
            assert result["exit_code"] == 0 and "ready" in result["output"]
            assert (await call(client, "exec_command", args))["job_id"] == job["job_id"]

            other = (
                await call(reviewer, "task_create", {"project": "local/project", "title": "review"})
            )["id"]
            pending = await call(reviewer, "exec_command", {**args, "task_id": other})
            assert pending["status"] == "pending_approval"
            assert pending["service"]["id"] == projects["service"]["id"]

            harness.runner.config.permissions.remove("execute")
            denied = await call(
                client, "exec_command", {**args, "request_id": "runner-denied"}, error=True
            )
            assert "not permitted" in denied["result"]["error"]
            owner.permissions.remove("execute")
            denied = await call(
                client, "exec_command", {**args, "request_id": "client-denied"}, error=True
            )
            assert "not permitted" in denied["error"]
            owner.trusted_projects.append("local/ungranted")
            await call(
                client, "task_create", {"project": "local/ungranted", "title": "denied"}, error=True
            )
    finally:
        await harness.close()


async def test_policy_change_releases_original_pending_job_and_revocation_blocks_dispatch(tmp_path):
    client = Client(
        token="c" * 40, projects=["runner/project", "runner/other"], project_roots=["runner/work"]
    )
    client.permissions.add("projects")
    config = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={"owner": client},
        runners={"runner": "r" * 40},
    )
    service = Service(config)
    service.store.register(
        "runner",
        "one",
        {"project": "a" * 64, "other": "b" * 64},
        project_roots={"work": {"binding": "c" * 64, "projects": []}},
    )
    tasks = [
        service.store.create_task("owner", "runner/" + project, title)
        for project, title in (("project", "trusted"), ("other", "review"), ("project", "closed"))
    ]
    jobs = [
        service.store.enqueue(task, "exec_command", {"argv": ["echo", "ready"]}, "once", True, 10)
        for task in tasks
    ]
    root_job = await service.call(
        "owner", "project_create", {"root": "runner/work", "name": "new", "request_id": "new"}
    )
    service.store.update_task(tasks[2]["id"], "owner", "completed", "done", [])
    identity = service.store.service["id"]
    service.store.close()
    client.trusted_projects = ["runner/project", "runner/work"]
    service = Service(config)
    try:
        assert service.store.service["id"] == identity
        released = service.store.job(jobs[0]["job_id"], "owner")
        assert released["status"] == "queued" and released["approved"] == 0
        for pending in [*jobs[1:], root_job]:
            assert service.store.job(pending["job_id"], "owner")["status"] == "pending_approval"
        assert "computer_control" in client.approvals_for("runner/project")
        assert "projects" in client.approvals_for("runner/work", "project")
        replay = service.store.enqueue(
            tasks[0], "exec_command", {"argv": ["echo", "ready"]}, "once", False, 10
        )
        assert replay["job_id"] == released["job_id"]
        client.trusted_projects = []
        assert service.claim("runner", "one", 10) == []
        assert service.store.job(released["job_id"], "owner")["status"] == "pending_approval"
        client.trusted_projects = ["runner/project"]
        service.release_pending_approvals()
        assert service.claim("runner", "one", 10)[0]["id"] == released["job_id"]
        events = service.store.rows("SELECT kind FROM events WHERE task_id=?", (tasks[0]["id"],))
        assert any(e["kind"] == "approval.policy_changed" for e in events)
        with pytest.raises(ValueError):
            Client(token="x" * 40, projects=[], trusted_projects=["runner/*"])
    finally:
        service.store.close()
