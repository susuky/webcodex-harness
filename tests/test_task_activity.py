from __future__ import annotations

import json
import sqlite3

from webcodex.store import Store


def observe(store, task, tool, process, status, request, created, updated):
    job = store.enqueue(task, tool, {}, request, False, 10)
    store.claim("runner", "instance", 1)
    result = {
        "process_id": process,
        "status": status,
        "exit_code": 3 if status == "failed" else None,
        "output": "x" * 65536,
    }
    store.complete("runner", job["job_id"], "instance", "succeeded", result)
    with store.db:
        store.db.execute(
            "UPDATE jobs SET created=?,updated=? WHERE id=?", (created, updated, job["job_id"])
        )
    return job["job_id"], result


def test_activity_keeps_latest_observation_and_process_start_order(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "observations")
        observe(store, task, "exec_command", "older", "running", "old-start", 10, 11)
        newest, result = observe(store, task, "skill_run", "newer", "failed", "new-start", 20, 21)
        observe(store, task, "read_process", "older", "completed", "old-read", 30, 31)
        observe(store, task, "cancel_process", "excluded", "completed", "ignored", 40, 41)
        with store.db:
            store.db.execute("UPDATE jobs SET status='unknown' WHERE request_id='ignored'")
        activity = store.task_activity(task)
        assert [
            (p["process_id"], p["status"], p["started_at"])
            for p in activity["process_observations"]
        ] == [
            ("newer", "failed", 20),
            ("older", "completed", 10),
        ]
        assert activity["latest_execution"]["job_id"] == newest
        assert activity["latest_execution"]["result"] == result
        assert activity["job_counts"] == {"succeeded": 3, "unknown": 1}
        statements = []
        store.db.set_trace_callback(statements.append)
        store.task_activity(task)
        store.db.set_trace_callback(None)
        observation_query = next(s for s in statements if "WITH observations" in s)
        assert "json_extract" not in observation_query and "json_type" not in observation_query
        plan = store.db.execute("EXPLAIN QUERY PLAN " + observation_query).fetchall()
        assert any("process_observations" in row[3] for row in plan)
    finally:
        store.close()


def test_old_database_backfills_once_and_preserves_raw_results(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "runner/project", "migration")
    job, result = observe(store, task, "exec_command", "process", "completed", "observed", 10, 11)
    invalid = store.enqueue(task, "read_process", {}, "invalid", False, 10)
    with store.db:
        store.db.execute(
            "UPDATE jobs SET status='succeeded',result='[1]' WHERE id=?", (invalid["job_id"],)
        )
    store.close()
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.execute("DROP INDEX process_observations")
        for column in ["process_id", "process_status", "process_exit_code"]:
            db.execute("ALTER TABLE jobs DROP COLUMN " + column)
    store = Store(tmp_path)
    try:
        activity = store.task_activity(task)
        assert activity["process_observations"][0]["process_id"] == "process"
        assert activity["latest_execution"]["job_id"] == job
        assert activity["latest_execution"]["result"] == result
        assert (
            store.job(invalid["job_id"], "owner")["result"]["error_type"] == "InvalidStoredResult"
        )
        assert (
            store.db.execute("SELECT result FROM jobs WHERE id=?", (invalid["job_id"],)).fetchone()[
                0
            ]
            == "[1]"
        )
        assert store.db.execute("SELECT result FROM jobs WHERE id=?", (job,)).fetchone()[
            0
        ] == json.dumps(result, ensure_ascii=False)
    finally:
        store.close()
    store = Store(tmp_path)
    try:
        assert store.task_activity(task) == activity
    finally:
        store.close()


def test_late_unknown_completion_updates_the_same_process_projection(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "late result")
        job = store.enqueue(task, "exec_command", {}, "once", False, 10)
        store.claim("runner", "instance", 1)
        store.complete("runner", job["job_id"], "instance", "unknown", {"error": "connection lost"})
        assert store.task_activity(task)["process_observations"] == []
        result = {"process_id": "process", "status": "completed", "exit_code": 0}
        store.complete("runner", job["job_id"], "instance", "succeeded", result)
        before = store.task_activity(task)
        store.complete("runner", job["job_id"], "instance", "failed", {"error": "duplicate"})
        assert store.task_activity(task) == before
        assert before["latest_execution"]["result"] == result
    finally:
        store.close()
