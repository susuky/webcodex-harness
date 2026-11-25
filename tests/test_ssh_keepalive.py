"""Reconnect the real child process without waiting for the next scheduled task."""

import importlib.util
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from webcodex.config import Client, ManagementSource, ServerConfig
from webcodex.server import create_app

CHILD_PYTHON = getattr(sys, "_base_executable", sys.executable)


def load_ssh_example(name):
    path = Path(__file__).resolve().parents[1] / "examples/windows/task-center" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ssh_keepalive():
    return load_ssh_example("ssh-heartbeat-client")


def catalog_result(at, status, *, source_id="windows", port=18769, transport_error=True):
    row = {
        "event": "http.completed",
        "route": "/api/manage/sources/:source_id/state",
        "method": "GET",
        "source_id": source_id,
        "time": datetime.fromtimestamp(at, UTC).isoformat(),
        "duration_ms": 300,
        "http_status": status,
    }
    if port is not None:
        row["source_loopback_port"] = port
    if status == 502:
        row["source_transport_error"] = transport_error
    return row


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    module = load_ssh_example("windows-ssh-watchdog")
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 1000))
    diagnostics, attempted = tmp_path / "requests", tmp_path / "attempt"

    def observe(*requests, started_at=940):
        diagnostics.write_text(
            "".join(json.dumps(row) + "\n" for row in requests),
            encoding="utf-8",
        )
        return module.catalog_needs_reconnect(diagnostics, attempted, started_at)

    return observe, attempted


def test_watchdog_reconnects_for_consecutive_fresh_catalog_transport_failures(watchdog):
    observe, _ = watchdog
    first, second = catalog_result(975, 502), catalog_result(995, 502)
    assert not observe(first)
    assert not observe(first, catalog_result(976, 502))
    assert not observe(first, second, started_at=980)
    assert not observe(catalog_result(945, 502), catalog_result(960, 502))
    assert not observe(first, catalog_result(990, 200), second)
    assert not observe(first, catalog_result(990, 502, transport_error=False), second)
    assert observe(first, second)


def test_watchdog_matches_the_forwarded_endpoint_across_source_names(watchdog):
    observe, _ = watchdog
    other_first = catalog_result(975, 502, port=18765)
    assert not observe(other_first, catalog_result(995, 502, port=None))
    assert not observe(other_first, catalog_result(995, 502))
    assert observe(
        catalog_result(975, 502),
        catalog_result(990, 200, port=18765),
        catalog_result(995, 502, source_id="desktop"),
    )


def test_watchdog_rearms_only_after_a_new_catalog_success(watchdog):
    observe, attempted = watchdog
    attempted.touch()
    os.utime(attempted, (970, 970))
    first, second = catalog_result(975, 502), catalog_result(995, 502)
    assert not observe(catalog_result(960, 200), first, second)
    assert attempted.exists()
    assert not observe(first, second, catalog_result(999, 200, port=18765))
    assert attempted.exists()
    assert not observe(first, second, catalog_result(999, 200, source_id="desktop"))
    assert not attempted.exists()
    assert observe(first, second)


async def test_catalog_completion_logs_the_selected_endpoint_and_transport_failure(
    tmp_path, monkeypatch
):
    clients = {
        owner: Client(
            token=owner * 16,
            projects=[],
            permissions={"read"},
            management_sources={
                "windows": ManagementSource(url=f"http://127.0.0.1:{port}", token="r" * 40)
            },
        )
        for owner, port in (("first", 18769), ("second", 18765))
    }
    app = create_app(
        ServerConfig(state_dir=tmp_path, admin_token="a" * 40, clients=clients, runners={})
    )
    sources = app.state.management_sources

    def upstream(request):
        if request.url.port == 18769:
            raise httpx.RemoteProtocolError("origin disconnected")
        return httpx.Response(
            200,
            json={
                "total_count": 0,
                "next_offset": None,
                "tasks": [],
                "runners": [],
                "projects": [],
                "observed_at": time.time(),
            },
        )

    @asynccontextmanager
    async def remote_client(owner, source_id):
        async with httpx.AsyncClient(
            base_url=clients[owner].management_sources[source_id].url,
            transport=httpx.MockTransport(upstream),
        ) as client:
            yield client

    monkeypatch.setattr(sources, "remote_client", remote_client)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as portal,
    ):
        for who, expected in (("first", 502), ("second", 200)):
            response = await portal.get(
                "/api/manage/sources/windows/state?window=10",
                headers={"Authorization": "Bearer " + clients[who].token},
            )
            assert response.status_code == expected

    path = tmp_path / "diagnostics/server.jsonl"
    requests = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [
        (row["http_status"], row["source_loopback_port"], row.get("source_transport_error"))
        for row in requests
        if row["event"] == "http.completed" and row.get("source_id") == "windows"
    ] == [(502, 18769, True), (200, 18765, None)]


def test_ssh_exit_restarts_child_and_preserves_heartbeats(tmp_path, monkeypatch, ssh_keepalive):
    attempts = tmp_path / "attempts.txt"
    child = tmp_path / "fake_ssh.py"
    child.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "p=Path(sys.argv[1])\n"
        "seen=p.read_text() if p.exists() else ''\n"
        "heartbeat=sys.stdin.buffer.readline()\n"
        "assert heartbeat == b'alive\\n'\n"
        "p.write_text(seen + 'connected\\n')\n"
        "raise SystemExit(255 if not seen else 0)\n",
        encoding="utf-8",
    )
    delays = []

    class StopProbe(Exception):
        pass

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise StopProbe

    monkeypatch.setattr(
        ssh_keepalive, "time", SimpleNamespace(sleep=sleep, strftime=ssh_keepalive.time.strftime)
    )
    with (tmp_path / "log").open("ab") as log, pytest.raises(StopProbe):
        ssh_keepalive.keep_connected([CHILD_PYTHON, str(child), str(attempts)], log)

    assert attempts.read_text() == "connected\nconnected\n"
    assert delays == [5, 5]
    text = (tmp_path / "log").read_text()
    assert "SSH exited 255" in text
    assert "SSH exited 0" in text


def test_session_failure_reaps_only_its_child(tmp_path, monkeypatch, ssh_keepalive):
    real_popen = ssh_keepalive.subprocess.Popen
    owned = []

    class StopProbe(Exception):
        pass

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        owned.append(process)
        real_wait = process.wait

        def wait(timeout=None):
            if timeout is not None:
                raise StopProbe
            return real_wait(timeout=5)

        process.wait = wait
        return process

    monkeypatch.setattr(ssh_keepalive.subprocess, "Popen", popen)
    with (tmp_path / "log").open("ab") as log, pytest.raises(StopProbe):
        ssh_keepalive.run_session([CHILD_PYTHON, "-c", "import time; time.sleep(30)"], log)

    assert len(owned) == 1
    assert owned[0].poll() is not None
    assert owned[0].stdin.closed


def test_closed_ssh_stdin_does_not_wait_for_a_live_child(tmp_path, monkeypatch, ssh_keepalive):
    monkeypatch.setattr(ssh_keepalive, "HEARTBEAT_SECONDS", 0.02)
    with (tmp_path / "log").open("ab") as log:
        code = ssh_keepalive.run_session(
            [CHILD_PYTHON, "-c", "import os,time; os.close(0); time.sleep(30)"], log
        )
    assert code is not None and code != 0


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows byte-range locks")
def test_reconnect_launcher_lock_is_exclusive_and_reusable(tmp_path, ssh_keepalive):
    path = tmp_path / "ssh-heartbeat-client.lock"
    with ssh_keepalive.instance_lock(path) as first:
        assert first
        with ssh_keepalive.instance_lock(path) as duplicate:
            assert not duplicate
    with ssh_keepalive.instance_lock(path) as replacement:
        assert replacement


def test_default_client_keeps_windows_openssh(monkeypatch, tmp_path, ssh_keepalive):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    arguments = ["-T", "-o", "StrictHostKeyChecking=yes", "host", "true"]
    assert ssh_keepalive.ssh_command(arguments) == [
        str(tmp_path / "System32/OpenSSH/ssh.exe"),
        *arguments,
    ]


def test_explicit_client_runs_with_unchanged_arguments(tmp_path, ssh_keepalive):
    received = tmp_path / "received.json"
    arguments = [
        "-o",
        "StrictHostKeyChecking=yes",
        "-i",
        r"C:\Keys with spaces\id_ed25519",
        "user@host",
        "exec /usr/bin/python3 ~/.config/webcodex-task-center/windows-ssh-watchdog.py",
    ]
    child = (
        "import json,sys;from pathlib import Path;"
        "assert sys.stdin.buffer.readline() == b'alive\\n';"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))"
    )
    command = ssh_keepalive.ssh_command(
        ["--ssh-executable", CHILD_PYTHON, "-c", child, str(received), *arguments]
    )
    with (tmp_path / "log").open("ab") as log:
        assert ssh_keepalive.run_session(command, log) == 0
    assert json.loads(received.read_text()) == arguments
