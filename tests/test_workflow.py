import sys
import time

import pytest
from test_harness import Harness, call

from webcodex.config import Client, ServerConfig
from webcodex.server import Service


async def test_mcp_resume_reports_failed_command_and_continues_one_process(tmp_path):
    harness = Harness(tmp_path)
    await harness.start()
    try:
        async with harness.mcp() as client:
            task = (
                await call(
                    client,
                    "task_create",
                    {
                        "project": "local/project",
                        "title": "workflow regression",
                    },
                )
            )["id"]
            await call(
                client,
                "task_update",
                {
                    "task_id": task,
                    "status": "waiting",
                    "summary": "舊摘要：等待 CLI 核准",
                    "plan": [],
                },
            )
            args = {
                "task_id": task,
                "request_id": "run-once",
                "yield_time_ms": 0,
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(1.2); print('test failed'); raise SystemExit(3)",
                ],
            }
            reply = await client.call_tool("exec_command", args)
            job = reply.structuredContent
            original_job = job["job_id"]
            output, processes = "", set()
            deadline = time.monotonic() + 15
            while True:
                assert not reply.isError  # The tool successfully delivered the process observation.
                assert job["status"] != "pending_approval" and job["approved"] == 0
                result = job.get("result") or {}
                output += result.get("output", "")
                if result.get("process_id"):
                    processes.add(result["process_id"])
                if "next_action" not in job:
                    break
                assert time.monotonic() < deadline
                action = job["next_action"]
                reply = await client.call_tool(action["tool"], action["arguments"])
                job = reply.structuredContent
            assert job["status"] == "succeeded" and job["outcome"] == "failed"
            assert job["result"]["exit_code"] == 3
            assert len(processes) == 1 and "started" in output and "test failed" in output
            assert (await call(client, "exec_command", args))["job_id"] == original_job

            resumed = await call(client, "task_get", {"task_id": task})
            progress = resumed["progress"]
            assert (
                resumed["status"] == "waiting"
            )  # Preserve the handoff, but don't mistake it for live state.
            assert progress["state"] == "needs_attention"
            assert progress["summary_stale"] and progress["approval_mode"] == "automatic"
            assert progress["pending_jobs"] == []
            assert progress["latest_execution"]["outcome"] == "failed"
            assert progress["process_observations"][0]["exit_code"] == 3
            assert "process_id" in progress["latest_execution"]["result"]
            listed = (await call(client, "task_list"))["tasks"][0]
            assert listed["next_action"] == {"tool": "task_get", "arguments": {"task_id": task}}

            later = await call(
                client,
                "exec_command",
                {
                    "task_id": task,
                    "argv": [sys.executable, "-c", "print('later run')"],
                },
            )
            await call(
                client, "read_process", {"task_id": task, "process_id": next(iter(processes))}
            )
            progress = (await call(client, "task_get", {"task_id": task}))["progress"]
            assert (
                progress["latest_execution"]["result"]["process_id"]
                == later["result"]["process_id"]
            )

            await call(
                client,
                "task_update",
                {
                    "task_id": task,
                    "status": "active",
                    "summary": "已查明測試失敗，接續修復",
                    "plan": [],
                },
            )
            progress = (await call(client, "task_get", {"task_id": task}))["progress"]
            assert not progress["summary_stale"] and progress["state"] == "ready"
    finally:
        await harness.close()


async def test_resume_finds_old_queued_job_and_distinguishes_offline_runner(tmp_path):
    owner = Client(token="c" * 40, projects=["local/project"])
    config = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="a" * 40,
        clients={"owner": owner, "other": Client(token="d" * 40, projects=["local/project"])},
        runners={"local": "r" * 40},
    )
    service = Service(config)
    store = service.store
    store.register("local", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "local/project", "old pending job")
    pending = await service.call(
        "owner",
        "exec_command",
        {
            "task_id": task["id"],
            "argv": ["echo", "once"],
            "request_id": "once",
        },
    )
    with store.db:
        store.db.execute(
            "UPDATE jobs SET status='pending_approval' WHERE id=?", (pending["job_id"],)
        )
    for index in range(35):
        job = store.enqueue(task, "read_file", {"path": "file.txt"}, str(index), 100)
        service.claim("local", "instance", 1)
        store.complete("local", job["job_id"], "instance", "succeeded", {"content": "ok"})
    resumed = await service.call("owner", "task_get", {"task_id": task["id"]})
    assert pending["job_id"] not in {job["job_id"] for job in resumed["jobs"]}
    progress = resumed["progress"]
    assert progress["state"] == "observing"
    assert progress["pending_jobs"][0]["job_id"] == pending["job_id"]
    assert "approval" not in progress["pending_jobs"][0]
    with pytest.raises(ValueError, match="Task not found"):
        await service.call("other", "task_get", {"task_id": task["id"]})
    identity = store.service["id"]
    store.close()

    service = Service(config)
    try:
        assert service.store.service["id"] == identity
        with service.store.db:
            service.store.db.execute("UPDATE runners SET seen=?", (time.time() - 30,))
        resumed = await service.call("owner", "task_get", {"task_id": task["id"]})
        progress = resumed["progress"]
        assert progress["state"] == "waiting_runner"
        assert progress["approval_mode"] == "automatic" and not progress["runner_online"]
        queued = progress["pending_jobs"][0]
        assert queued["job_id"] == pending["job_id"] and queued["status"] == "queued"
        assert not queued["runner_online"] and "approval" not in queued
        claimed = service.claim("local", "instance", 1)
        assert len(claimed) == 1 and claimed[0]["id"] == pending["job_id"]
        service.store.complete(
            "local",
            pending["job_id"],
            "instance",
            "succeeded",
            {
                "process_id": "process",
                "status": "completed",
                "exit_code": 0,
                "output": "done",
                "next_cursor": 4,
                "has_more": True,
            },
        )
        done = await service.call("owner", "job_get", {"job_id": pending["job_id"]})
        assert done["outcome"] == "completed"
        assert done["next_action"]["arguments"]["cursor"] == 4
        service.store.register("local", "instance", {"project": "x" * 64})
        resumed = await service.call("owner", "task_get", {"task_id": task["id"]})
        assert resumed["progress"]["state"] == "project_changed"
    finally:
        service.store.close()
