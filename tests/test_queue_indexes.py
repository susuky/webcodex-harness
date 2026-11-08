from __future__ import annotations

import json
import sqlite3

import pytest

from webcodex.config import RunnerConfig
from webcodex.runner import Runner
from webcodex.store import Store

pytest_plugins = ["test_harness"]


def test_pending_limit_stays_bounded_after_history_migration(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "instance", {"project": "a" * 64})
    task = store.create_task("owner", "runner/project", "history")
    other = store.create_task("other", "runner/project", "other owner")
    with store.db:
        store.db.execute("DROP INDEX pending_jobs")
        store.db.executemany(
            "INSERT INTO jobs(id,task_id,owner,runner,project,binding,tool,arguments,status,created,updated) "
            "VALUES (?,?,?,'runner','project',?,'read_file','{}',?,1,1)",
            (
                (
                    f"{index:032x}",
                    task["id"] if index % 2 else other["id"],
                    "owner" if index % 2 else "other",
                    "a" * 64,
                    "succeeded" if index % 2 else "queued",
                )
                for index in range(20000)
            ),
        )
    pending = [store.enqueue(task, "read_file", {}, str(i), 10) for i in range(3)]
    with store.db:
        for job, status in zip(pending, ["queued", "running", "pending_approval"], strict=True):
            store.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["job_id"]))
    before = store.db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    store.close()

    store = Store(tmp_path)
    try:
        assert store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == before
        # A scan of the historical rows exhausts this SQLite instruction budget.
        store.db.set_progress_handler(lambda: 1, 5000)
        with pytest.raises(ValueError, match="Too many unfinished jobs"):
            store.enqueue(task, "read_file", {}, "blocked", 3)
        store.db.set_progress_handler(None, 0)
        with store.db:
            for job, status in zip(pending, ["succeeded", "failed", "cancelled"], strict=True):
                store.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job["job_id"]))
        created = store.enqueue(task, "read_file", {}, "next", 1)
        assert store.enqueue(task, "read_file", {}, "next", 1)["job_id"] == created["job_id"]
        with pytest.raises(ValueError, match="Too many unfinished jobs"):
            store.enqueue(task, "read_file", {}, "another", 1)
    finally:
        store.db.set_progress_handler(None, 0)
        store.close()


async def test_delivery_migration_keeps_history_and_flushes_only_unacknowledged(harness):
    config = RunnerConfig(
        server_url=harness.url,
        runner_id="local",
        token="r" * 40,
        state_dir=harness.config.state_dir.parent / "legacy-runner",
        projects={"project": harness.repo},
    )
    config.state_dir.mkdir()
    with sqlite3.connect(config.state_dir / "deliveries.sqlite3") as db:
        db.execute(
            "CREATE TABLE deliveries (id TEXT PRIMARY KEY,instance TEXT NOT NULL,"
            "status TEXT NOT NULL,result TEXT,acknowledged INTEGER NOT NULL DEFAULT 0)"
        )
        db.executemany(
            "INSERT INTO deliveries VALUES (?,'previous','succeeded',?,1)",
            ((f"{i:032x}", json.dumps({"output": "history" * 100})) for i in range(20000)),
        )
    runner = Runner(config)
    store = harness.app.state.service.store
    task = store.create_task("owner", "local/project", "delivery migration")
    job = store.enqueue(task, "read_file", {}, "pending", 10)
    # Exercise the existing running instance through real HTTP, without replacing it.
    runner.instance = harness.runner.instance
    store.claim("local", runner.instance, 1)
    with runner.db:
        runner.db.execute(
            "INSERT INTO deliveries VALUES (?,?,'succeeded',?,0)",
            (job["job_id"], runner.instance, json.dumps({"content": "delivered"})),
        )
    try:
        async with harness.http("r" * 40) as client:
            runner.db.set_progress_handler(lambda: 1, 5000)
            await runner.register(client)
            await runner.flush(client)
            await runner.flush(client)
            runner.db.set_progress_handler(None, 0)
        assert store.job(job["job_id"], "owner")["result"] == {"content": "delivered"}
        assert runner.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 20001
        assert (
            runner.db.execute("SELECT count(*) FROM deliveries WHERE acknowledged=0").fetchone()[0]
            == 0
        )
        assert runner.db.execute(
            "SELECT result FROM deliveries WHERE id=?", (f"{0:032x}",)
        ).fetchone()[0] == json.dumps({"output": "history" * 100})
    finally:
        runner.db.set_progress_handler(None, 0)
        await runner.processes.close()
        for extension in runner.extensions.values():
            await extension.close()
        runner.db.close()
        runner.lock_file.close()
