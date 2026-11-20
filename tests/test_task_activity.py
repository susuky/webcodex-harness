from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from webcodex.store import Store


def observe(store, task, tool, process, status, request, created, updated, job_status="succeeded"):
    with patch("webcodex.store.time.time", return_value=created):
        job = store.enqueue(task, tool, {}, request, 10)
        store.claim("runner", "instance", 1)
    result = {
        "process_id": process,
        "status": status,
        "exit_code": 3 if status == "failed" else None,
        "output": "x" * 65536,
    }
    with patch("webcodex.store.time.time", return_value=updated):
        store.complete("runner", job["job_id"], "instance", job_status, result)
    return job["job_id"], result


def test_activity_keeps_latest_observation_and_process_start_order(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "observations")
        observe(store, task, "exec_command", "older", "running", "old-start", 10, 11)
        newest, result = observe(store, task, "skill_run", "newer", "failed", "new-start", 20, 21)
        observe(store, task, "read_process", "older", "completed", "old-read", 30, 31)
        observe(
            store, task, "cancel_process", "excluded", "completed", "ignored", 40, 41, "unknown"
        )
        activity = store.task_activity(task)
        assert [
            (p["process_id"], p["status"], p["started_at"], p["tool"])
            for p in activity["process_observations"]
        ] == [
            ("newer", "failed", 20, "skill_run"),
            ("older", "completed", 10, "read_process"),
        ]
        assert activity["latest_execution"]["job_id"] == newest
        assert activity["latest_execution"]["result"] == result
        assert "job_counts" not in activity
        statements = []
        store.db.set_trace_callback(statements.append)
        store.task_activity(task)
        store.db.set_trace_callback(None)
        observation_query = next(s for s in statements if "FROM process_states" in s)
        assert "json_extract" not in observation_query and "json_type" not in observation_query
        plan = store.db.execute("EXPLAIN QUERY PLAN " + observation_query).fetchall()
        assert any("recent_process_states" in row[3] for row in plan)
    finally:
        store.close()


def test_activity_can_omit_execution_output_without_loading_job(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "compact progress")
        observe(store, task, "exec_command", "process", "running", "observed", 10, 11)
        full = store.task_activity(task)
        assert full["latest_execution"]["result"]["output"] == "x" * 65536

        def unexpected_job(*args):
            raise AssertionError("Compact activity must not load the complete execution")

        monkeypatch.setattr(store, "job", unexpected_job)
        compact = store.task_activity(task, include_output=False)
        assert compact == {**full, "latest_execution": None}
    finally:
        store.close()


def test_old_database_backfills_once_and_preserves_raw_results(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "runner/project", "migration")
    job, result = observe(store, task, "exec_command", "process", "completed", "observed", 10, 11)
    invalid = store.enqueue(task, "read_process", {}, "invalid", 10)
    with store.db:
        store.db.execute(
            "UPDATE jobs SET status='succeeded',result='[1]' WHERE id=?", (invalid["job_id"],)
        )
    store.close()
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        db.execute("DROP TABLE process_states")
        db.execute("DROP INDEX process_observations")
        db.execute("DROP INDEX process_history")
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
        job = store.enqueue(task, "exec_command", {}, "once", 10)
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


@pytest.mark.parametrize("legacy", [False, True])
def test_terminal_process_survives_delayed_running_observation_and_migration(tmp_path, legacy):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "Delayed delivery")
        terminal, _ = observe(store, task, "read_process", "process", "failed", "done", 20, 30)
        delayed, _ = observe(store, task, "exec_command", "process", "running", "start", 10, 40)
        activity = store.task_activity(task)
        assert activity["process_observations"] == [
            {
                "job_id": terminal,
                "tool": "read_process",
                "process_id": "process",
                "status": "failed",
                "exit_code": 3,
                "started_at": 10,
                "observed_at": 30,
            }
        ]
        assert activity["work"]["running_count"] == 0
        assert store.job(delayed, "owner")["outcome"] == "running"
        with store.db:
            if legacy:
                store.db.execute("DROP TABLE process_states")
            else:
                store.db.execute("DELETE FROM metadata WHERE key='terminal_process_states'")
                store.db.execute(
                    "UPDATE process_states SET job_id=?,tool='exec_command',status='running',"
                    "exit_code=NULL,observed_at=40",
                    (delayed,),
                )
    finally:
        store.close()
    store = Store(tmp_path)
    try:
        assert store.task_activity(task) == activity
    finally:
        store.close()


def test_current_process_migration_uses_metadata_and_keeps_legacy_results(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.register("runner", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "runner/project", "Current process migration")
    observe(store, task, "exec_command", "normal", "running", "start", 10, 11)
    observe(store, task, "read_process", "normal", "completed", "done", 20, 21)
    job = store.enqueue(task, "subagent_spawn", {}, "legacy", 10)
    store.claim("runner", "instance", 1)
    store.complete(
        "runner",
        job["job_id"],
        "instance",
        "succeeded",
        {"agent_id": "legacy", "status": "running"},
    )
    expected = store.rows("SELECT * FROM process_states ORDER BY process_id")
    store.close()
    connect = sqlite3.connect
    with connect(tmp_path / "state.sqlite3") as db:
        db.execute(
            "CREATE TABLE agent_observations AS SELECT task_id,process_id AS agent_id,"
            "job_id,status,exit_code,started_at AS created,observed_at AS updated "
            "FROM process_states WHERE tool='subagent_get'"
        )
        db.execute("DROP TABLE process_states")

    def metadata_connection(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, table, column, database, source):
            if (
                action == sqlite3.SQLITE_READ
                and table == "jobs"
                and column in {"result", "arguments"}
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr("webcodex.store.sqlite3.connect", metadata_connection)
    migrated = Store(tmp_path)
    try:
        assert migrated.rows("SELECT * FROM process_states ORDER BY process_id") == expected
        assert (
            migrated.one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_observations'"
            )
            is None
        )
        migrated.db.set_authorizer(None)
        assert migrated.job(job["job_id"], "owner")["result"] == {
            "agent_id": "legacy",
            "status": "running",
        }
    finally:
        migrated.close()
