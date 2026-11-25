"""Reconnect the real child process without waiting for the next scheduled task."""

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    module = load_ssh_example("windows-ssh-watchdog")
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 1000))
    diagnostics, incidents, attempted = (
        tmp_path / name for name in ("requests", "incidents", "attempt")
    )

    def observe(requests, errors, *, started_at=940):
        diagnostics.write_text(
            "".join(
                json.dumps(
                    {
                        "event": "http.completed",
                        "route": "/api/manage/sources/:source_id/state",
                        "method": "GET",
                        "source_id": "windows",
                        "time": datetime.fromtimestamp(at, UTC).isoformat(),
                        "duration_ms": 300,
                        "http_status": status,
                    }
                )
                + "\n"
                for at, status in requests
            ),
            encoding="utf-8",
        )
        incidents.write_text(
            "".join(
                json.dumps(
                    {
                        "component": "source",
                        "key": "state",
                        "source_id": "windows",
                        **error,
                    }
                )
                + "\n"
                for error in errors
            ),
            encoding="utf-8",
        )
        return module.catalog_needs_reconnect(diagnostics, incidents, attempted, started_at)

    return observe, attempted


def test_watchdog_reconnects_for_consecutive_fresh_catalog_transport_failures(watchdog):
    observe, _ = watchdog
    timeouts = [{"started_at": 970, "reason": "source_timeout"}]
    assert not observe([(975, 502)], timeouts)
    assert not observe([(975, 502), (976, 502)], timeouts)
    assert not observe([(975, 502), (995, 502)], timeouts, started_at=980)
    assert not observe([(975, 502), (990, 200), (995, 502)], timeouts)
    assert observe([(975, 502), (995, 502)], timeouts)

    disconnect = {"started_at": 988, "reason": "source_invalid_response"}
    changed = [{**timeouts[0], "closed_at": 988}, disconnect]
    assert not observe([(975, 502), (995, 502)], changed)
    disconnect["details"] = {"exception_type": "RemoteProtocolError"}
    assert observe([(975, 502), (995, 502)], changed)
    disconnect["reason"] = "source_auth_failed"
    assert not observe([(975, 502), (995, 502)], changed)


def test_watchdog_rearms_only_after_a_new_catalog_success(watchdog):
    observe, attempted = watchdog
    attempted.touch()
    os.utime(attempted, (970, 970))
    timeouts = [{"started_at": 971, "reason": "source_read_timeout"}]
    assert not observe([(960, 200), (975, 502), (995, 502)], timeouts)
    assert attempted.exists()
    assert not observe([(975, 502), (995, 502), (999, 200)], timeouts)
    assert not attempted.exists()
    assert observe([(975, 502), (995, 502)], timeouts)


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
