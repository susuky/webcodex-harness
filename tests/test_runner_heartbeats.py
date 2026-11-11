from __future__ import annotations

from webcodex.store import Store


def test_idle_polling_keeps_online_lease_without_repeated_disk_commits(tmp_path, monkeypatch):
    now = 1000.0
    monkeypatch.setattr("webcodex.store.time.time", lambda: now)
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "a" * 64})
        commits = []
        store.db.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
        for tick in range(1, 151):
            now = 1000.0 + tick / 10
            assert store.claim("runner", "instance", 1) == []
            assert store.inventory()[0]["online"] is True
            seen = store.one("SELECT seen FROM runners WHERE id=?", ("runner",))["seen"]
            assert 0 <= now - seen < 5
        assert len(commits) == 3
        now += 15
        assert store.inventory()[0]["online"] is False
    finally:
        store.close()


def test_job_dispatch_and_capacity_heartbeat_do_not_wait_for_write_interval(tmp_path, monkeypatch):
    now = 1000.0
    monkeypatch.setattr("webcodex.store.time.time", lambda: now)
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "a" * 64})
        task = store.create_task("owner", "runner/project", "Immediate dispatch")
        now += 0.1
        job = store.enqueue(task, "read_file", {}, "next-job", 10)
        assert [item["id"] for item in store.claim("runner", "instance", 1)] == [job["job_id"]]
        assert store.job(job["job_id"], "owner")["status"] == "running"
        queued = store.enqueue(task, "read_file", {}, "waiting-job", 10)
        now += 5
        assert store.claim("runner", "instance", 0) == []
        assert store.one("SELECT seen FROM runners WHERE id=?", ("runner",))["seen"] == now
        assert store.job(queued["job_id"], "owner")["status"] == "queued"
        assert [item["id"] for item in store.claim("runner", "instance", 1)] == [queued["job_id"]]
    finally:
        store.close()
