from __future__ import annotations

import json
import sqlite3

import httpx

from webcodex.config import Client, ManagementSource, ServerConfig
from webcodex.server import create_app

pytest_plugins = ["test_harness", "test_task_liveness"]


async def test_remote_reason_change_keeps_both_causes_without_false_recovery(harness, tmp_path):
    history = harness.app.state.service.connections
    first = history.fail(
        "server", "fixture-event-loop", "event_loop_blocked", details={"query": "SELECT private"}
    )
    second = history.fail(
        "server",
        "fixture-event-loop",
        "monitor_scheduling_gap",
        details={"gap_seconds": 500, "previous_observed_at": 1, "observed_at": 501},
    )
    portal = create_app(
        ServerConfig(
            state_dir=tmp_path / "portal",
            admin_token="a" * 40,
            runners={},
            clients={
                "owner": Client(
                    token="p" * 40,
                    projects=[],
                    permissions={"read"},
                    management_sources={
                        "windows": ManagementSource(url=harness.url, token="c" * 40)
                    },
                )
            },
        )
    )
    async with portal.router.lifespan_context(portal):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=portal),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + "p" * 40},
        ) as client:
            for path in ("/api/manage/connections", "/api/manage/sources/windows/connections"):
                response = await client.get(path)
                assert response.status_code == 200
                rows = response.json()["incidents"]
                previous = next(row for row in rows if row["id"] == first)
                current = next(row for row in rows if row["id"] == second)
                assert previous["state"] == "changed"
                assert previous["recovered_at"] is None
                assert previous["closed_at"] == current["started_at"]
                assert previous["end_reason"] == "diagnostic_changed"
                assert current["state"] == "active"
                assert current["details"]["gap_seconds"] == 500
                assert all(row["source_id"] == "windows" for row in rows)
                assert "SELECT private" not in json.dumps(rows)


def test_job_activity_mapping_does_not_read_saved_payloads(activity_service):
    service, task = activity_service
    job = service.store.enqueue(task, "exec_command", {}, "activity-metadata", 100)

    def metadata_only(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and table == "jobs" and column in {"arguments", "result"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    service.store.db.set_authorizer(metadata_only)
    try:
        for tool in ("job_get", "job_cancel"):
            assert (
                service.tool_activity_task("owner", tool, {"job_id": job["job_id"]}) == task["id"]
            )
            assert service.tool_activity_task("other", tool, {"job_id": job["job_id"]}) is None
            assert service.tool_activity_task("owner", tool, {"job_id": "missing"}) is None
    finally:
        service.store.db.set_authorizer(None)
