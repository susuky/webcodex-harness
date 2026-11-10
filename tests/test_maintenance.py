from __future__ import annotations

import json
import os
import sqlite3
import time
import tracemalloc

import httpx
import pytest

from webcodex.config import initialize
from webcodex.maintenance import backup, doctor, retention, update_check
from webcodex.store import Store


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    directory = tmp_path / "private"
    initialize(directory, repo, 19877)
    process_dir = directory / "runner" / "processes"
    process_dir.mkdir(parents=True)
    with sqlite3.connect(process_dir / "processes.sqlite3") as db:
        db.execute(
            "CREATE TABLE processes(id TEXT PRIMARY KEY, task_id TEXT, status TEXT, created REAL, finished REAL)"
        )
    with sqlite3.connect(directory / "runner" / "deliveries.sqlite3") as db:
        db.execute(
            "CREATE TABLE deliveries(id TEXT PRIMARY KEY,status TEXT,result TEXT,acknowledged INTEGER)"
        )
    return directory


def test_update_check_reads_real_journals_without_changing_status(installation):
    directory = installation
    with sqlite3.connect(directory / "runner" / "processes" / "processes.sqlite3") as db:
        db.execute("INSERT INTO processes VALUES ('process','task','running',1,NULL)")
    with sqlite3.connect(directory / "runner" / "deliveries.sqlite3") as db:
        db.execute("INSERT INTO deliveries VALUES ('delivery','running',NULL,0)")
    result = update_check(directory)
    assert result["runner_restart_ready"] is False
    assert result["running_processes"][0]["process_id"] == "process"
    assert result["running_deliveries"] == ["delivery"]
    with sqlite3.connect(directory / "runner" / "processes" / "processes.sqlite3") as db:
        assert db.execute("SELECT status FROM processes").fetchone()[0] == "running"


def test_update_check_missing_config_is_not_ready(tmp_path):
    report = update_check(tmp_path)
    assert not report["runner_restart_ready"]
    assert report["errors"] == ["missing_runner_config"]


def test_doctor_malformed_client_returns_check_error(installation):
    (installation / "client.json").write_text('{"server_url":null,"token":1}')
    report = doctor(installation, share=True)
    assert any(c.get("error") == "ValueError" for c in report["checks"])


def test_share_report_does_not_include_paths_tokens_or_raw_connection_errors(
    installation, monkeypatch
):
    secret = json.loads((installation / "client.json").read_text())["token"]

    def broken(*args, **kwargs):
        raise httpx.ConnectError("private URL " + str(installation) + " token " + secret)

    monkeypatch.setattr(httpx, "Client", broken)
    report = doctor(installation, share=True)
    encoded = json.dumps(report)
    assert str(installation) not in encoded and secret not in encoded
    assert report["shareable"] is True
    assert any(c.get("error") == "ConnectError" for c in report["checks"])
    assert isinstance(report["computer"].get("available"), bool)


def test_backup_has_valid_sqlite_snapshots_and_private_config(installation):
    store = Store(installation / "server")
    store.close()
    result = backup(installation)
    from pathlib import Path

    dest = Path(result["backup"])
    assert json.loads((dest / "client.json").read_text()) == json.loads(
        (installation / "client.json").read_text()
    )
    for name in result["databases"]:
        with sqlite3.connect(dest / name) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if os.name != "nt":
        assert (dest / "client.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        backup(installation, dest)


def _job(store, task, request, payload):
    job = store.enqueue(task, "computer_screenshot", {}, request, 20)
    store.claim("local", "instance", 1)
    store.complete("local", job["job_id"], "instance", "succeeded", payload)
    with store.db:
        store.db.execute(
            "UPDATE jobs SET updated=? WHERE id=?", (time.time() - 50 * 86400, job["job_id"])
        )
    return job["job_id"]


def test_retention_preserves_active_unknown_and_metadata_and_prunes_completed_images(installation):
    store = Store(installation / "server")
    store.register("local", "instance", {"project": "b" * 64})
    completed = store.create_task("owner", "local/project", "finished")
    active = store.create_task("owner", "local/project", "active")
    uncertain = store.create_task("owner", "local/project", "uncertain")
    image = {
        "mcp_result": {"content": [{"type": "image", "mimeType": "image/png", "data": "A" * 20000}]}
    }
    jid = _job(store, completed, "complete", image)
    active_jid = _job(store, active, "active", image)
    unknown_jid = _job(store, uncertain, "unknown", image)
    store.update_task(completed["id"], "owner", "completed", "done", [])
    store.update_task(uncertain["id"], "owner", "completed", "uncertain", [])
    with store.db:
        store.db.execute("UPDATE jobs SET status='unknown' WHERE id=?", (unknown_jid,))
    with sqlite3.connect(installation / "runner" / "deliveries.sqlite3") as db:
        db.execute("INSERT INTO deliveries VALUES (?,?,?,1)", (jid, "succeeded", json.dumps(image)))
    before = store.job(jid, "owner")
    dry = retention(installation, days=30, max_mib=1)
    assert dry["dry_run"] and dry["selected_bytes"] > 10000
    assert "retention" not in store.job(jid, "owner")["result"]
    applied = retention(installation, days=30, max_mib=1, apply=True)
    after = store.job(jid, "owner")
    assert after["owner"] == before["owner"] and after["task_id"] == before["task_id"]
    assert after["status"] == before["status"] and after["updated"] == before["updated"]
    assert after["result"]["retention"]["original_bytes"] > 10000
    assert "retention" not in store.job(active_jid, "owner")["result"]
    assert "retention" not in store.job(unknown_jid, "owner")["result"]
    assert applied["reclaimed_payload_bytes"] > 30000
    with sqlite3.connect(installation / "runner" / "deliveries.sqlite3") as db:
        assert json.loads(
            db.execute("SELECT result FROM deliveries WHERE id=?", (jid,)).fetchone()[0]
        )["retention"]
    store.close()


def test_retention_logs_artifacts_profiles_and_links(installation, tmp_path):
    store = Store(installation / "server")
    store.register("local", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "local/project", "finished")
    store.update_task(task["id"], "owner", "completed", "", [])
    logs = installation / "runner" / "processes"
    old = time.time() - 40 * 86400
    with sqlite3.connect(logs / "processes.sqlite3") as db:
        db.executemany(
            "INSERT INTO processes VALUES (?,?,?,?,?)",
            [
                ("a" * 32, task["id"], "completed", old, old),
                ("b" * 32, "other-active-task", "running", old, None),
            ],
        )
    for pid in ("a" * 32, "b" * 32):
        log = logs / (pid + ".log")
        log.write_text("output")
        os.utime(log, (old, old))
    session = (
        installation
        / "runner"
        / "extensions"
        / "project"
        / "sessions"
        / "browser-tools"
        / "browser"
        / task["id"]
    )
    artifacts = session / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "screen.png").write_bytes(b"image")
    os.utime(artifacts / "screen.png", (old, old))
    (session / "profile").mkdir()
    (session / "profile" / "cookies").write_text("private")
    external = tmp_path / "keep.txt"
    external.write_text("keep")
    try:
        (artifacts / "link").symlink_to(external)
    except OSError:
        pass
    result = retention(installation, days=30, apply=True, save_policy=True)
    assert result["removed_file_bytes"] == len("outputimage")
    assert not (logs / ("a" * 32 + ".log")).exists()
    assert (logs / ("b" * 32 + ".log")).exists()
    assert (session / "profile" / "cookies").exists() and external.exists()
    assert json.loads((installation / "retention.json").read_text())["days"] == 30
    store.close()


def test_retention_rejects_invalid_policy(installation):
    with pytest.raises(ValueError):
        retention(installation, days=0)


@pytest.mark.parametrize("apply", [False, True])
def test_retention_memory_is_bounded_by_one_payload(installation, apply):
    store = Store(installation / "server")
    try:
        store.register("local", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "local/project", "many screenshots")
        image = {
            "mcp_result": {
                "content": [{"type": "image", "mimeType": "image/png", "data": "A" * (512 * 1024)}]
            }
        }
        ids = [_job(store, task, str(index), image) for index in range(24)]
        store.update_task(task["id"], "owner", "completed", "", [])
        tracemalloc.start()
        try:
            result = retention(installation, days=30, apply=apply)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result["selected_bytes"] > 10 * 1024 * 1024
        assert peak < 6 * 1024 * 1024, f"Retained payloads in memory: {peak} bytes"
        assert ("retention" in store.job(ids[0], "owner")["result"]) is apply
    finally:
        store.close()


def test_doctor_reports_invalid_fields_without_private_input(installation, monkeypatch):
    path = installation / "server.json"
    config = json.loads(path.read_text())
    original = next(iter(config["clients"].values()))
    config["clients"] = {"private-account-name": {**original, "token": "secret-short-token"}}
    path.write_text(json.dumps(config))

    def disconnected(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", disconnected)
    report = doctor(installation, share=True)
    failure = next(check for check in report["checks"] if check["name"] == "server_config")
    assert not failure["ok"]
    assert failure["fields"] == [
        {"field": "clients.*.token", "type": "string_too_short", "constraints": {"min_length": 32}}
    ]
    encoded = json.dumps(report)
    assert "private-account-name" not in encoded and "secret-short-token" not in encoded
    assert "input" not in failure and "ctx" not in failure


def test_doctor_reports_json_error_position_without_source_text(installation, monkeypatch):
    (installation / "runner.json").write_text('{"private-location":\n bad-secret-value}')

    def disconnected(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", disconnected)
    report = doctor(installation, share=True)
    failure = next(check for check in report["checks"] if check["name"] == "runner_config")
    assert failure["error"] == "JSONDecodeError" and failure["line"] == 2
    assert failure["column"] > 0
    encoded = json.dumps(report)
    assert "private-location" not in encoded and "bad-secret-value" not in encoded
