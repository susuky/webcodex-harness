from __future__ import annotations

import sqlite3

import pytest

pytest_plugins = ["test_harness"]


async def test_duplicate_completion_ack_never_reads_or_serializes_payloads(harness, monkeypatch):
    store = harness.app.state.service.store
    task = store.create_task("owner", "local/project", "Duplicate completion")
    job = store.enqueue(task, "read_file", {"path": "a" * (2 * 1024 * 1024)}, "once", 10)
    instance = harness.runner.instance
    store.claim("local", instance, 1)
    accepted = {"output": "original" * (300 * 1024)}
    store.complete("local", job["job_id"], instance, "succeeded", accepted)
    saved = tuple(store.db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
    original_complete = store.complete
    reads = []

    def authorize(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "jobs":
            reads.append(column)
            if column in {"result", "arguments", "created"}:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def unexpected_json(*args, **kwargs):
        raise AssertionError("Acknowledgement must not encode or decode result/arguments JSON")

    def complete_without_payloads(*args, **kwargs):
        changes = store.db.total_changes
        store.db.set_authorizer(authorize)
        try:
            with monkeypatch.context() as patch:
                patch.setattr("webcodex.store.json.loads", unexpected_json)
                patch.setattr("webcodex.store.json.dumps", unexpected_json)
                return original_complete(*args, **kwargs)
        finally:
            store.db.set_authorizer(None)
            assert store.db.total_changes == changes

    monkeypatch.setattr(store, "complete", complete_without_payloads)
    with pytest.raises(ValueError, match="Invalid Runner completion"):
        store.complete(
            "local",
            job["job_id"],
            instance,
            "succeeded",
            {"process_id": "process", "status": "bogus"},
        )
    body = {
        "job_id": job["job_id"],
        "instance": instance,
        "status": "failed",
        "result": {"output": "ignored" * (300 * 1024)},
    }
    async with harness.http("r" * 40) as client:
        invalid = await client.post(
            "/api/runner/complete",
            json={**body, "result": {"process_id": "process", "status": "bogus"}},
        )
        assert invalid.status_code == 400
        wrong_instance = await client.post(
            "/api/runner/complete", json={**body, "instance": "another"}
        )
        assert wrong_instance.status_code == 400
        duplicate = await client.post("/api/runner/complete", json=body)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"accepted": True}
    assert set(reads) == {"id", "runner", "instance", "status"}
    assert (
        tuple(store.db.execute("SELECT * FROM jobs WHERE id=?", (job["job_id"],)).fetchone())
        == saved
    )
    assert store.job(job["job_id"], "owner")["result"] == accepted
