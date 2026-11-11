"""Reconnect the real child process without waiting for the next scheduled task."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

CHILD_PYTHON = getattr(sys, "_base_executable", sys.executable)


@pytest.fixture
def ssh_keepalive():
    path = (
        Path(__file__).resolve().parents[1] / "examples/windows/task-center/ssh-heartbeat-client.py"
    )
    spec = importlib.util.spec_from_file_location("ssh_keepalive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
