"""Release this SSH transport after heartbeat loss or repeated catalog transport failures."""

import http.client
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

FORWARD_PORT = 18769
LOG_TAIL_BYTES = 64 * 1024
FAILURE_WINDOW = 120
TRANSPORT_FAILURES = {
    "source_connect_timeout",
    "source_read_timeout",
    "source_timeout",
    "source_unreachable",
}


def recent_records(path):
    try:
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > LOG_TAIL_BYTES:
                stream.seek(-LOG_TAIL_BYTES, os.SEEK_END)
                stream.readline()  # Discard the possibly partial first record.
            for line in stream.read(LOG_TAIL_BYTES).splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # The logger can still be writing the last line.
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return


def catalog_needs_reconnect(diagnostics, incidents, attempted, started_at):
    now = time.time()
    try:
        attempted_at = attempted.stat().st_mtime
    except FileNotFoundError:
        attempted_at = None
    failures = []
    for row in recent_records(diagnostics):
        if (
            row.get("event") != "http.completed"
            or row.get("route") != "/api/manage/sources/:source_id/state"
            or row.get("source_id") != "windows"
            or row.get("method") != "GET"
        ):
            continue
        try:
            observed = datetime.fromisoformat(row["time"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        duration = row.get("duration_ms")
        if not isinstance(duration, (int, float)):
            continue
        began = observed - duration / 1000
        if began < started_at or not now - FAILURE_WINDOW <= observed <= now:
            continue
        status = row.get("http_status")
        if status == 502:
            failures.append(observed)
        else:
            failures.clear()
            if status == 200 and attempted_at is not None and began > attempted_at:
                attempted.unlink(missing_ok=True)
                attempted_at = None

    if (
        len(failures) < 2
        or failures[-1] - failures[0] < 10
        or now - failures[-1] > 30
        or attempted_at is not None
    ):
        return False

    # The HTTP log counts fresh attempts. The incident log classifies each outcome.
    history = [
        row
        for row in recent_records(incidents)
        if row.get("component") == "source"
        and row.get("source_id") == "windows"
        and row.get("key") == "state"
        and isinstance(row.get("started_at"), (int, float))
    ]
    for observed in reversed(failures):
        incident = next(
            (
                row
                for row in reversed(history)
                if row["started_at"] - 0.001
                <= observed
                < (row.get("recovered_at") or row.get("closed_at") or now + 1)
            ),
            {},
        )
        reason = incident.get("reason")
        if reason not in TRANSPORT_FAILURES and not (
            reason == "source_invalid_response"
            and (incident.get("details") or {}).get("exception_type") == "RemoteProtocolError"
        ):
            break
        if failures[-1] - observed >= 10:
            return True
    return False


def owns_forward(parent):
    group = Path(f"/proc/{parent}/cgroup").read_text().strip().removeprefix("0::")
    scope = Path(group).name
    if not (scope.startswith("session-") and scope.endswith(".scope")):
        return False
    listeners = subprocess.run(
        ["ss", "-H", "-ltnpe", f"sport = :{FORWARD_PORT}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    ).stdout.splitlines()
    return any(
        f"127.0.0.1:{FORWARD_PORT}" in line.split() and f"cgroup:{group}" in line.split()
        for line in listeners
    )


def check_origin():
    try:
        with closing(http.client.HTTPConnection("127.0.0.1", FORWARD_PORT, timeout=6)) as client:
            client.request("GET", "/healthz")
            response = client.getresponse()
            if response.status == 200:
                payload = json.loads(response.read(1024))
                if (
                    payload["status"] == "ok"
                    and payload["service"]["product"] == "webcodex-harness"
                ):
                    return "origin_healthy"
    except TimeoutError:
        # A completely stalled data path may hide the origin. Retry it at most once.
        return "health_read_timeout"
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    parent = os.getppid()
    proc = Path(f"/proc/{parent}")
    if (
        parent <= 1
        or proc.stat().st_uid != os.getuid()
        or (proc / "comm").read_text().strip() != "sshd"
    ):
        raise RuntimeError("The watchdog must be an SSH session command.")
    handle = os.pidfd_open(parent)
    try:
        if os.getppid() != parent:
            raise RuntimeError("The SSH session ended during watchdog setup.")
        state = Path(__file__).resolve().parent / "state"
        diagnostics = state / "diagnostics/server.jsonl"
        incidents = state / "connections/events.jsonl"
        attempted = state / "connections/windows-ssh-reconnected"
        started_at = time.time()
        last_write = None
        incoming = sys.stdin.fileno()
        while select.select([incoming], [], [], 45)[0]:
            if not os.read(incoming, 4096):
                break
            try:
                changed_at = diagnostics.stat().st_mtime_ns
                if changed_at == last_write:
                    continue
                last_write = changed_at
                if not catalog_needs_reconnect(diagnostics, incidents, attempted, started_at):
                    continue
                if not owns_forward(parent):
                    continue
                origin = check_origin()
                if origin and catalog_needs_reconnect(
                    diagnostics, incidents, attempted, started_at
                ):
                    attempted.touch(mode=0o600, exist_ok=False)
                    print(
                        f"Reconnecting task-center SSH after catalog failures ({origin})",
                        file=sys.stderr,
                    )
                    break
            except FileNotFoundError:
                continue
            except (OSError, TypeError, subprocess.SubprocessError) as error:
                print(
                    f"Task center SSH recovery check failed: {type(error).__name__}",
                    file=sys.stderr,
                )
        try:
            signal.pidfd_send_signal(handle, signal.SIGTERM)
        except ProcessLookupError:
            pass
    finally:
        os.close(handle)


if __name__ == "__main__":
    main()
