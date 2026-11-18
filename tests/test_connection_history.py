from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from webcodex.config import Client, ManagementSource, ServerConfig
from webcodex.connections import ConnectionHistory
from webcodex.management_sources import SourceUnavailable
from webcodex.server import create_app


def snapshot(history, owner="owner", sources=("local", "windows"), runners=("local",)):
    return history.snapshot(owner=owner, source_ids=set(sources), runner_ids=set(runners))


def test_connection_reason_survives_recovery_restart_and_hides_private_details(tmp_path):
    history = ConnectionHistory(tmp_path)
    incident_id = history.fail(
        "source",
        "state",
        "source_read_timeout",
        source_id="windows",
        owner="owner",
        details={
            "duration_ms": 15000,
            "operation": "state",
            "token": "private-token",
            "url": "https://private.test?token=secret",
            "query": "SELECT seen FROM runners WHERE id=?",
            "stack": ["store.py:10:one"],
            "location": "store.py:10",
        },
    )
    assert (
        history.fail("source", "state", "source_read_timeout", source_id="windows", owner="owner")
        == incident_id
    )
    assert len(history.path.read_text().splitlines()) == 1
    history.recover(
        "source",
        "state",
        source_id="windows",
        owner="owner",
        details={"duration_ms": 12, "query": "SELECT 1", "stack": ["store.py:12:one"]},
    )
    restored = ConnectionHistory(tmp_path)
    recovered = snapshot(restored)["incidents"]
    assert len(recovered) == 1
    assert recovered[0]["id"] == incident_id
    assert recovered[0]["reason"] == "source_read_timeout"
    assert recovered[0]["started_at"] <= recovered[0]["recovered_at"]
    assert recovered[0]["state"] == "recovered"
    assert recovered[0]["recovery_details"] == {"duration_ms": 12}
    assert recovered[0]["details"] == {"duration_ms": 15000, "operation": "state"}
    assert "private-token" not in history.path.read_text()
    assert "private.test" not in history.path.read_text()
    assert snapshot(history, owner="other")["incidents"] == []
    evidence = restored.snapshot(owner="owner", source_ids={"windows"}, include_evidence=True)
    assert evidence["incidents"][0]["details"]["query"] == "SELECT seen FROM runners WHERE id=?"
    assert (
        restored.snapshot(owner="other", source_ids={"windows"}, include_evidence=True)["incidents"]
        == []
    )
    if os.name != "nt":
        assert history.path.stat().st_mode & 0o777 == 0o600
        assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_source_failure_changes_are_durable_without_reporting_a_recovery(tmp_path):
    history = ConnectionHistory(tmp_path)
    scope = {"source_id": "windows", "owner": "owner"}
    timeout = history.fail(
        "source",
        "state",
        "source_read_timeout",
        **scope,
        details={"operation": "state", "duration_ms": 15000},
    )
    protocol = history.fail(
        "source",
        "state",
        "source_invalid_response",
        **scope,
        details={
            "operation": "state",
            "exception_type": "RemoteProtocolError",
            "duration_ms": 2045,
        },
    )
    assert protocol != timeout
    for duration in range(1000, 1050):
        assert (
            history.fail(
                "source",
                "state",
                "source_invalid_response",
                **scope,
                details={
                    "operation": "state",
                    "exception_type": "RemoteProtocolError",
                    "duration_ms": duration,
                    "last_seen": time.time(),
                    "age_seconds": duration / 100,
                },
            )
            == protocol
        )
    restored = ConnectionHistory(tmp_path)
    rows = snapshot(restored)["incidents"]
    previous = next(row for row in rows if row["id"] == timeout)
    current = next(row for row in rows if row["id"] == protocol)
    assert previous["state"] == "changed"
    assert previous["recovered_at"] is None
    assert previous["end_reason"] == "diagnostic_changed"
    assert previous["closed_at"] == current["started_at"]
    assert current["state"] == "active"
    assert current["details"]["exception_type"] == "RemoteProtocolError"
    assert snapshot(restored, owner="other")["incidents"] == []
    restored.recover("source", "state", **scope)
    rows = snapshot(ConnectionHistory(tmp_path))["incidents"]
    assert next(row for row in rows if row["id"] == timeout)["recovered_at"] is None
    assert next(row for row in rows if row["id"] == protocol)["state"] == "recovered"


def test_same_reason_changes_only_for_fixed_diagnostics_and_bounds_closed_history(tmp_path):
    history = ConnectionHistory(tmp_path, limit=1)
    first = history.fail(
        "server",
        "event-loop",
        "event_loop_blocked",
        details={"query": "SELECT count(*) FROM jobs WHERE task_id=?", "duration_ms": 2000},
    )
    assert (
        history.fail(
            "server",
            "event-loop",
            "event_loop_blocked",
            details={"query": "SELECT count(*) FROM jobs WHERE task_id=?", "duration_ms": 3000},
        )
        == first
    )
    second = history.fail(
        "server",
        "event-loop",
        "event_loop_blocked",
        details={"query": "SELECT seen FROM runners", "duration_ms": 3000},
    )
    assert second != first
    third = history.fail(
        "server",
        "event-loop",
        "event_loop_blocked",
        details={"query": "SELECT id FROM runners", "duration_ms": 3000},
    )
    assert third != second
    restored = ConnectionHistory(tmp_path, limit=1)
    rows = snapshot(restored)["incidents"]
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {second, third}
    assert next(row for row in rows if row["id"] == second)["state"] == "changed"
    assert "query" not in rows[0]["details"]


def test_connection_history_keeps_active_incidents_and_serializes_watchdog_threads(tmp_path):
    history = ConnectionHistory(tmp_path, limit=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(
            pool.map(
                lambda _: history.fail("server", "event-loop", "event_loop_blocked"), range(40)
            )
        )
    assert len(set(ids)) == 1
    for number in range(4):
        history.fail("runner", str(number), "runner_heartbeat_expired", runner_id=str(number))
        history.recover("runner", str(number), runner_id=str(number))
    restored = ConnectionHistory(tmp_path, limit=1)
    rows = restored.snapshot(owner="owner", source_ids={"local"})["incidents"]
    assert len(rows) == 2
    assert any(row["id"] == ids[0] and row["state"] == "active" for row in rows)
    assert all(row["runner_id"] != "3" for row in snapshot(restored)["incidents"])


def test_connection_recording_failure_preserves_memory_and_next_transition_saves(
    tmp_path, monkeypatch
):
    history = ConnectionHistory(tmp_path)
    with monkeypatch.context() as patch:

        def denied(*args, **kwargs):
            raise PermissionError("private filesystem details")

        patch.setattr("webcodex.connections.tempfile.mkstemp", denied)
        history.fail("server", "event-loop", "event_loop_blocked")
        state = snapshot(history)
        assert len(state["incidents"]) == 1
        assert state["persisted"] is False
        assert state["recording_error"] == "PermissionError"
        assert "private filesystem" not in json.dumps(state)
    history.recover("server", "event-loop")
    assert snapshot(history)["persisted"] is True
    assert snapshot(ConnectionHistory(tmp_path))["incidents"][0]["state"] == "recovered"


async def test_real_source_failure_recovery_history_scope_and_legacy_diagnostics(tmp_path):
    mode = {"failed": True, "diagnostics": False, "diagnostics_failed": False}
    requests = []

    async def upstream(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        path = header.split(b" ")[1].decode().split("?", 1)[0]
        requests.append(path)
        status, payload = (
            200,
            {"tasks": [], "runners": [], "observed_at": time.time(), "next_offset": None},
        )
        if path.startswith("/api/manage/tasks/"):
            status, payload = 404, {"error": "missing task"}
        elif path == "/api/manage/sources/local/connections":
            if mode["diagnostics_failed"]:
                status, payload = 503, {"error": "private service failure"}
            elif not mode["diagnostics"]:
                status, payload = 404, {"error": "old Server"}
            else:
                payload = {
                    "incidents": [
                        {
                            "id": "f" * 32,
                            "component": "server",
                            "source_id": "local",
                            "runner_id": None,
                            "started_at": 123,
                            "recovered_at": 130,
                            "reason": "event_loop_blocked",
                            "details": {"duration_ms": 2500},
                            "recovery_details": {"duration_ms": 9000, "query": "SELECT secret"},
                        }
                    ],
                    "runners": [{"id": "local", "last_seen": time.time(), "online": True}],
                    "persisted": True,
                }
        elif mode["failed"]:
            status, payload = 503, {"error": "private upstream output token=secret"}
        body = json.dumps(payload).encode()
        writer.write(
            f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    config = ServerConfig(
        state_dir=tmp_path / "state",
        admin_token="admin" * 8,
        runners={},
        clients={
            "owner": Client(
                token="owner" * 8,
                projects=[],
                permissions={"read"},
                management_sources={
                    "windows": ManagementSource(
                        url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                        token="source" * 8,
                    )
                },
            ),
            "other": Client(token="other" * 8, projects=[], permissions={"read"}),
        },
    )
    app = create_app(config)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "owner" * 8},
        ) as client:
            assert (await client.get("/api/manage/sources/windows/state")).status_code == 502
            assert (
                await client.get("/api/manage/sources/windows/tasks/missing")
            ).status_code == 404
            history = (await client.get("/api/manage/connections")).json()
            assert history["incidents"][0]["reason"] == "source_http_error"
            assert history["incidents"][0]["state"] == "active"
            assert history["incidents"][0]["details"]["http_status"] == 503
            assert history["sources"][-1]["status"] == "unsupported"
            assert "token=secret" not in json.dumps(history)
            assert (await client.get("/api/manage/connections?limit=0")).status_code == 400
            assert (
                await client.get(
                    "/api/manage/connections", headers={"Authorization": "Bearer " + "other" * 8}
                )
            ).json()["incidents"] == []
            assert (
                await client.get(
                    "/api/manage/sources/windows/connections",
                    headers={"Authorization": "Bearer " + "other" * 8},
                )
            ).status_code == 400
            mode.update(failed=False, diagnostics=True)
            assert (await client.get("/api/manage/sources/windows/state")).status_code == 200
            app.state.management_sources.connection_cache.clear()
            report = (await client.get("/api/manage/sources/windows/connections")).json()
            source_incident = next(
                row for row in report["incidents"] if row["component"] == "source"
            )
            assert source_incident["state"] == "recovered"
            remote_incident = next(
                row for row in report["incidents"] if row["component"] == "server"
            )
            assert remote_incident["source_id"] == "windows"
            assert remote_incident["recovery_details"] == {"duration_ms": 9000}
            assert report["sources"][0]["status"] == "available"
            count = len(requests)
            await client.get("/api/manage/sources/windows/connections")
            assert len(requests) == count
            evidence = (
                await client.get("/api/manage/sources/windows/connections?evidence=1")
            ).json()
            evidence_server = next(
                row for row in evidence["incidents"] if row["component"] == "server"
            )
            assert evidence_server["recovery_details"]["query"] == "SELECT secret"
            assert (await client.get("/api/manage/connections?evidence=no")).status_code == 400
            mode["diagnostics_failed"] = True
            for key, (cached_at, cached_data) in list(
                app.state.management_sources.connection_cache.items()
            ):
                app.state.management_sources.connection_cache[key] = (cached_at - 16, cached_data)
            unavailable = (await client.get("/api/manage/sources/windows/connections")).json()
            assert unavailable["sources"][0]["status"] == "unavailable"
            assert unavailable["sources"][0]["reason"] == "source_http_error"
            assert len(unavailable["incidents"]) == 2
            assert unavailable["runners"][0]["stale"] is True
            assert "private service failure" not in json.dumps(unavailable)
            assert all(path.startswith("/api/manage/") for path in requests)
        restored = ConnectionHistory(config.state_dir / "connections")
        assert snapshot(restored)["incidents"][0]["state"] == "recovered"
    finally:
        await app.state.management_sources.close()
        app.state.service.store.close()
        app.state.service.diagnostics.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "error,reason",
    [
        (httpx.ConnectTimeout("private address"), "source_connect_timeout"),
        (httpx.ReadTimeout("private address"), "source_read_timeout"),
        (httpx.RemoteProtocolError("private response"), "source_invalid_response"),
    ],
)
def test_source_transport_failure_identifies_phase_without_exception_messages(error, reason):
    failure = SourceUnavailable.transport(error, "來源連線失敗")
    assert failure.reason == reason
    assert "private" not in json.dumps(failure.details)
