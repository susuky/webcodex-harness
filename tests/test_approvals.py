import sys

import pytest
from test_harness import Harness, call, process_result

from webcodex.config import Client, ServerConfig
from webcodex.server import Service


async def test_default_execution_needs_no_trust_setting_and_keeps_permissions(tmp_path):
    harness = Harness(tmp_path)
    await harness.start()
    try:
        async with harness.mcp() as client, harness.mcp("d" * 40) as other:
            for connection in (client, other):
                projects = await call(connection, "projects_list")
                assert projects["projects"][0]["approval_mode"] == "automatic"
                assert projects["projects"][0]["approval_required"] == []
                task = (
                    await call(
                        connection, "task_create", {"project": "local/project", "title": "direct"}
                    )
                )["id"]
                args = {
                    "task_id": task,
                    "argv": [sys.executable, "-c", "print('ready')"],
                    "request_id": "once",
                }
                job = await call(connection, "exec_command", args)
                assert job["status"] == "succeeded" and "approval" not in job
                result = await process_result(connection, task, job)
                assert result["exit_code"] == 0 and "ready" in result["output"]
                assert (await call(connection, "exec_command", args))["job_id"] == job["job_id"]
            harness.runner.config.permissions.remove("execute")
            denied = await call(
                other, "exec_command", {**args, "request_id": "runner-denied"}, error=True
            )
            assert "not permitted" in denied["result"]["error"]
            harness.config.clients["reviewer"].permissions.remove("execute")
            count = harness.app.state.service.store.one("SELECT count(*) AS n FROM jobs")["n"]
            denied = await call(
                other, "exec_command", {**args, "request_id": "client-denied"}, error=True
            )
            assert "not permitted" in denied["error"]
            assert (
                harness.app.state.service.store.one("SELECT count(*) AS n FROM jobs")["n"] == count
            )
            await call(
                client, "task_create", {"project": "local/ungranted", "title": "denied"}, error=True
            )
    finally:
        await harness.close()


def test_legacy_approval_settings_do_not_restore_a_gate_or_change_permissions():
    client = Client(
        token="c" * 40,
        projects=["runner/project"],
        permissions={"read"},
        approval_required=["execute"],
        trusted_projects=["runner/*"],
        trusted_project_roots=["runner/work"],
    )
    assert client.permissions == {"read"}
    assert not (
        {"approval_required", "trusted_projects", "trusted_project_roots"}
        & client.model_dump().keys()
    )
    with pytest.raises(ValueError, match="Unknown permission"):
        Client(token="c" * 40, projects=[], permissions={"unknown"})


async def test_upgrade_requeues_original_jobs_once_and_cancels_revoked_or_closed_tasks(tmp_path):
    client = Client(token="c" * 40, projects=["runner/project", "runner/other"])
    config = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={"owner": client},
        runners={"runner": "r" * 40},
    )
    service = Service(config)
    service.store.register("runner", "one", {"project": "a" * 64, "other": "b" * 64})
    tasks = [
        service.store.create_task("owner", "runner/" + project, title)
        for project, title in (("project", "active"), ("other", "revoked"), ("project", "closed"))
    ]
    jobs = [
        service.store.enqueue(task, "exec_command", {"argv": ["echo", "ready"]}, "once", 10)
        for task in tasks
    ]
    service.store.update_task(tasks[2]["id"], "owner", "completed", "done", [])
    with service.store.db:
        service.store.db.execute("UPDATE jobs SET status='pending_approval'")
    identity = service.store.service["id"]
    service.store.close()
    client.projects.remove("runner/other")
    service = Service(config)
    try:
        assert service.store.service["id"] == identity
        first = service.store.job(jobs[0]["job_id"], "owner")
        assert first["status"] == "queued" and "approval" not in first
        for job in jobs[1:]:
            assert service.store.job(job["job_id"], "owner")["status"] == "cancelled"
        replay = service.store.enqueue(
            tasks[0], "exec_command", {"argv": ["echo", "ready"]}, "once", 10
        )
        assert replay["job_id"] == first["job_id"]
        service.resume_legacy_jobs()
        events = service.store.rows("SELECT id FROM events WHERE kind='job.legacy_queue_migrated'")
        assert len(events) == 3
        assert [job["id"] for job in service.claim("runner", "one", 10)] == [first["job_id"]]
    finally:
        service.store.close()
