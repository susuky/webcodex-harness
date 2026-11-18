from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import urllib.request
from types import SimpleNamespace

from webcodex.connections import ConnectionHistory
from webcodex.diagnostics import ConnectionMonitor
from webcodex.management_sources import SourceUnavailable
from webcodex.store import Store

pytest_plugins = ["test_harness"]


def test_loop_pulse_before_watchdog_preserves_the_unobserved_gap(tmp_path, monkeypatch):
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(
        "webcodex.diagnostics.time",
        SimpleNamespace(
            monotonic=lambda: clock["monotonic"],
            time=lambda: clock["wall"],
        ),
    )
    history = ConnectionHistory(tmp_path)
    monitor = ConnectionMonitor(SimpleNamespace(rows=lambda sql: []), history)
    monitor._loop = SimpleNamespace(call_later=lambda *args: None)
    monitor._tick()
    clock.update(monotonic=1900.0, wall=2800.0)
    monitor._tick()
    clock.update(monotonic=1900.5, wall=2800.5)
    monitor._tick()
    assert not history.path.exists()  # The event-loop pulse performs no journal I/O.
    waits = iter([False, True])
    monitor._stop = SimpleNamespace(wait=lambda _: next(waits))
    monitor._watch()
    rows = history.snapshot(owner="owner", source_ids={"local"})["incidents"]
    assert len(rows) == 1
    gap = rows[0]
    assert gap["reason"] == "monitor_scheduling_gap"
    assert gap["details"] == {
        "gap_seconds": 1800.0,
        "previous_observed_at": 1000.0,
        "observed_at": 2800.0,
    }
    assert gap["recovered_at"] is not None
    assert "query" not in gap["details"] and "stack" not in gap["details"]


async def test_blocked_sql_records_its_location_and_recovers_without_parameters(tmp_path):
    store = Store(tmp_path / "state")
    history = ConnectionHistory(tmp_path / "connections")
    monitor = ConnectionMonitor(store, history, interval=0.01, threshold=0.04)
    secret = "private-sql-parameter-that-must-not-be-recorded"
    store.db.create_function("pause_query", 1, lambda value: time.sleep(0.2))
    monitor.start()
    try:
        store.one("SELECT pause_query(?) AS paused", (secret,))
        await asyncio.sleep(0.08)
        incident = history.incidents[0]
        assert incident["reason"] == "event_loop_blocked"
        assert incident["recovered_at"] is not None
        assert incident["details"]["query"] == "SELECT pause_query(?) AS paused"
        assert incident["details"]["location"].startswith("store.py:")
        assert incident["details"]["stack"]
        assert incident["details"]["sampled_at"] <= incident["started_at"]
        assert incident["recovery_details"]["duration_ms"] >= 200
        assert secret not in history.path.read_text()
        restored = ConnectionHistory(tmp_path / "connections")
        public = restored.snapshot(owner="owner", source_ids={"local"})
        assert public["incidents"][0]["id"] == incident["id"]
        assert "query" not in public["incidents"][0]["details"]
        assert "stack" not in public["incidents"][0]["details"]
    finally:
        monitor.close()
        store.close()


async def test_async_wait_keeps_loop_responsive_and_does_not_record_an_outage(tmp_path):
    store = Store(tmp_path / "state")
    history = ConnectionHistory(tmp_path / "connections")
    monitor = ConnectionMonitor(store, history, interval=0.01, threshold=0.05)
    monitor.start()
    try:
        await asyncio.sleep(0.2)
        assert history.incidents == []
    finally:
        monitor.close()
        store.close()


async def test_runner_snapshot_error_does_not_stop_loop_monitor(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    history = ConnectionHistory(tmp_path / "connections")
    rows = store.rows

    def fail_once(sql, params=()):
        monkeypatch.setattr(store, "rows", rows)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "rows", fail_once)
    monitor = ConnectionMonitor(store, history, interval=0.01, threshold=0.08)
    monitor.start()
    try:
        await asyncio.sleep(0.1)
        assert history.incidents[0]["reason"] == "runner_status_unavailable"
        await asyncio.sleep(5.1)
        assert history.incidents[0]["recovered_at"] is not None
        assert all(row["reason"] != "event_loop_blocked" for row in history.incidents)
    finally:
        monitor.close()
        store.close()


async def test_mcp_connection_status_and_manage_routes_leave_jobs_untouched(harness):
    service = harness.app.state.service
    service.connections.fail(
        "server",
        "event-loop",
        "event_loop_blocked",
        details={"duration_ms": 2500, "query": "SELECT ? AS value", "stack": ["store.py:210 one"]},
    )
    before = service.store.one("SELECT count(*) AS n FROM jobs")["n"]
    async with harness.http() as client:
        response = await client.post(
            "/api/call", json={"name": "connection_status", "arguments": {}}
        )
        assert response.status_code == 200
        assert response.json()["incidents"][0]["reason"] == "event_loop_blocked"
        assert response.json()["incidents"][0]["details"]["query"] == "SELECT ? AS value"
        await client.get("/api/manage/sources/windows/tasks/private-task?private-query=secret")
    assert service.store.one("SELECT count(*) AS n FROM jobs")["n"] == before
    records = [
        json.loads(line)
        for line in (harness.config.state_dir / "diagnostics/server.jsonl").read_text().splitlines()
    ]
    request = next(
        row for row in records if row.get("route") == "/api/manage/sources/:source_id/tasks/:id"
    )
    assert request["source_id"] == "windows"
    assert "private-task" not in json.dumps(records)
    assert "private-query" not in json.dumps(records)


async def test_slow_incident_disk_write_does_not_block_real_http_health(harness, monkeypatch):
    service = harness.app.state.service
    entered, release = threading.Event(), threading.Event()
    save = service.connections._save

    def slow_disk():
        entered.set()
        assert release.wait(2)
        save()

    def health_while_writing():
        assert entered.wait(2)
        try:
            with urllib.request.urlopen(harness.url + "/healthz", timeout=0.5) as response:
                return response.status
        finally:
            release.set()

    monkeypatch.setattr(service.connections, "_save", slow_disk)
    probe = asyncio.create_task(asyncio.to_thread(health_while_writing))
    await asyncio.sleep(0)
    try:
        await service.sources.source_failed(
            "owner",
            "windows",
            "state",
            SourceUnavailable("timeout", "source_read_timeout"),
            time.monotonic(),
        )
        assert await probe == 200
    finally:
        release.set()
        await asyncio.gather(probe, return_exceptions=True)
