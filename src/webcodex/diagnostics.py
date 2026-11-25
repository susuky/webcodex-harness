from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import private_dir

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_EVENTS = {
    f"{kind}.{phase}"
    for kind in ("http", "tool", "resource", "discovery", "bridge")
    for phase in ("started", "completed", "cancelled", "error", "disconnected")
} | {"progress.error", "activity.error"}
_IDS = {"trace_id", "bridge_trace_id", "http_id", "task_id", "job_id", "wait_id"}
_HASHES = {"rpc_id_hash", "resource_hash", "owner_hash", "window_hash"}
_ENUMS = {
    "method": {"GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"},
    "route": {
        "/healthz",
        "/mcp",
        "/mcp/",
        "/api/call",
        "/api/tools",
        "/api/runner/register",
        "/api/runner/poll",
        "/api/runner/complete",
        "/api/admin/approvals",
        "/api/admin/approvals/:id",
        "/api/admin/questions",
        "/api/admin/questions/:id",
        "/api/manage",
        "/api/manage/session",
        "/api/manage/login",
        "/api/manage/logout",
        "/api/manage/state",
        "/api/manage/refresh",
        "/api/manage/connections",
        "/api/manage/sources",
        "/api/manage/tasks/:id",
        "/api/manage/tasks/:id/status",
        "/api/manage/tasks/:id/archive",
        "/api/manage/sources/:source_id/state",
        "/api/manage/sources/:source_id/refresh",
        "/api/manage/sources/:source_id/connections",
        "/api/manage/sources/:source_id/tasks/:id",
        "/api/manage/sources/:source_id/tasks/:id/handoff",
        "/api/manage/sources/:source_id/tasks/:id/archive",
        "/api/manage/sources/:source_id/tasks/:id/status",
        "unknown",
    },
    "rpc_method": {
        "initialize",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/read",
        "notifications/cancelled",
        "unknown",
    },
    "outcome": {
        "started",
        "completed",
        "returned",
        "cancelled",
        "error",
        "disconnected",
        "rejected",
        "queued",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "accepted",
        "pending_approval",
    },
    "transport": {"http", "mcp", "stdio"},
    "action": {"bind", "state", "prepare", "finish", "pause", "enable", "unbind"},
    "delivery_outcome": {"accepted", "unknown"},
    "delivery_error": {"timeout", "rpc_error", "host_rejected", "interrupted", "invalid_prepare"},
    "process_outcome": {
        "running",
        "completed",
        "failed",
        "timed_out",
        "interrupted",
        "cancelled",
        "unknown",
    },
    "wait_state": {"watching", "ready", "prepared", "accepted", "unknown", "consumed", "cancelled"},
    "control_reason": {"missing_window", "read_only", "replaced_view", "another_view"},
    "handoff_state": {
        "not_armed",
        "available",
        "blocked",
        "watching",
        "ready",
        "prepared",
        "accepted",
        "unknown",
        "consumed",
        "cancelled",
    },
    "handoff_reason": {
        "completed",
        "unsupported_task",
        "read_only",
        "paused",
        "project_changed",
        "waiting_approval",
        "waiting_operator",
        "waiting_runner",
        "missing_window",
        "not_connected",
        "another_window",
        "expired",
        "incomplete_job_list",
        "too_many_jobs",
        "no_pending_jobs",
    },
}
HTTP_ROUTES = _ENUMS["route"]


def hash_id(value: str | int | None) -> str | None:
    if type(value) not in (str, int):
        return None
    return hashlib.sha256(str(value).encode("utf-8", errors="surrogatepass")).hexdigest()


class _QuietStreamHandler(logging.StreamHandler):
    def handleError(self, record):
        pass


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def handleError(self, record):
        pass


class Diagnostics:
    """Bounded request metadata; callers choose tool names from their known tool catalog."""

    def __init__(self, component: str, directory: Path | None = None):
        self.component = (
            component
            if isinstance(component, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", component)
            else "unknown"
        )
        self.logger = logging.Logger("webcodex.diagnostics." + self.component, logging.INFO)
        self.logger.propagate = False
        handler = None
        if directory is not None:
            try:
                handler = _PrivateRotatingFileHandler(
                    private_dir(directory) / (self.component + ".jsonl"),
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
            except Exception:
                pass
        handler = handler or _QuietStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)

    def record(self, event: str, **fields):
        try:
            if event not in _EVENTS:
                return
            record = {
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "component": self.component,
                "event": event,
            }
            for key, value in fields.items():
                if key in _IDS and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
                    record[key] = value
                elif (
                    key in _HASHES
                    and isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", value)
                ):
                    record[key] = value
                elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
                    record[key] = value
                elif (
                    key in {"source_id", "runner_id"}
                    and isinstance(value, str)
                    and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value)
                ):
                    record[key] = value
                elif (
                    key == "tool"
                    and isinstance(value, str)
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
                ):
                    record[key] = value
                elif (
                    key == "exception_type"
                    and isinstance(value, str)
                    and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", value)
                ):
                    record[key] = value
                elif (
                    key in {"is_error", "disconnected", "control_bound", "source_transport_error"}
                    and type(value) is bool
                ):
                    record[key] = value
                elif key == "source_loopback_port" and type(value) is int and 1 <= value <= 65535:
                    record[key] = value
                elif (
                    key == "controller_generation"
                    and type(value) is int
                    and 0 <= value <= 2**53 - 1
                ):
                    record[key] = value
                elif key == "http_status" and type(value) is int and 100 <= value <= 599:
                    record[key] = value
                elif (
                    key in {"delivery_error_code", "exit_code"}
                    and type(value) is int
                    and -(2**31) <= value < 2**31
                ):
                    record[key] = value
                elif (
                    key in {"duration_ms", "response_bytes"}
                    and type(value) in (int, float)
                    and math.isfinite(value)
                    and 0 <= value <= 1e15
                ):
                    record[key] = value
            self.logger.info(json.dumps(record, ensure_ascii=True, allow_nan=False))
        except Exception:
            pass

    def close(self):
        for handler in self.logger.handlers[:]:
            try:
                handler.close()
            except Exception:
                pass
            self.logger.removeHandler(handler)


class ConnectionMonitor:
    """Capture a blocked event loop from another thread, without pausing it."""

    def __init__(self, store, history, *, interval: float = 0.5, threshold: float = 2):
        self.store, self.history = store, history
        self.interval, self.threshold = interval, threshold
        self._beat = time.monotonic()
        self._beat_wall = time.time()
        self._pending_gap = None
        self._pulse_lock = threading.Lock()
        self._runners = []
        self._runners_observed_at = 0.0
        self._runner_check = 0.0
        self._runner_error = None
        self._stop = threading.Event()
        self._timer = None
        self._thread = None
        self._loop_thread = None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._thread = threading.Thread(
            target=self._watch, name="webcodex-connections", daemon=True
        )
        self._thread.start()
        self._tick()

    def _tick(self):
        now, observed_at = time.monotonic(), time.time()
        with self._pulse_lock:
            gap = now - self._beat
            # Retain the largest missed interval until the observer runs again.
            if gap >= self.threshold and (
                self._pending_gap is None or gap > self._pending_gap["gap_seconds"]
            ):
                self._pending_gap = {
                    "gap_seconds": gap,
                    "previous_observed_at": self._beat_wall,
                    "observed_at": observed_at,
                }
            self._beat, self._beat_wall = now, observed_at
        try:
            if self._beat - self._runner_check >= 5:
                try:
                    self._runners = self.store.rows("SELECT id,seen FROM runners")
                    self._runners_observed_at = time.time()
                    self._runner_error = None
                except sqlite3.Error as error:
                    self._runner_error = type(error).__name__
                self._runner_check = self._beat
        finally:
            self._timer = self._loop.call_later(self.interval, self._tick)

    def _blocked_details(self, delay: float) -> dict:
        frame = sys._current_frames().get(self._loop_thread)
        stack, details = (
            [],
            {
                "duration_ms": delay * 1000,
                "pid": os.getpid(),
                "sampled_at": time.time(),
            },
        )
        while frame is not None:
            filename = Path(frame.f_code.co_filename)
            if filename.parent.name == "webcodex":
                location = f"{filename.name}:{frame.f_lineno}"
                operation = frame.f_code.co_name
                stack.append(f"{location} {operation}")
                if "location" not in details:
                    details.update(location=location, operation=operation)
                if filename.name == "store.py" and operation in {"one", "rows"}:
                    # Store SQL is a template; never read or retain its bound parameters.
                    query = frame.f_locals.get("sql")
                    if isinstance(query, str):
                        details["query"] = query[:4000]
                if operation == "traced_call":
                    tool = frame.f_locals.get("name")
                    if isinstance(tool, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool):
                        details["tool"] = tool
                if operation == "__call__":
                    fields = frame.f_locals.get("fields")
                    route = fields.get("route") if isinstance(fields, dict) else None
                    if route in _ENUMS["route"]:
                        details["route"] = route
            frame = frame.f_back
        details["stack"] = stack[:20]
        return details

    def _watch(self):
        blocked_at = None
        while not self._stop.wait(self.interval):
            with self._pulse_lock:
                beat, gap = self._beat, self._pending_gap
                self._pending_gap = None
            if gap is not None and blocked_at is None:
                # The observer missed this interval; a stack sampled now cannot explain it.
                self.history.fail("server", "monitor-gap", "monitor_scheduling_gap", details=gap)
                self.history.recover("server", "monitor-gap")
            delay = time.monotonic() - beat
            if delay >= self.threshold:
                if blocked_at is None:
                    details = self._blocked_details(delay)
                    if self._beat != beat:
                        continue
                    blocked_at = beat
                    self.history.fail("server", "event-loop", "event_loop_blocked", details=details)
            else:
                details = (
                    {"duration_ms": (time.monotonic() - blocked_at) * 1000} if blocked_at else None
                )
                self.history.recover("server", "event-loop", details=details)
                blocked_at = None
            if self._runner_error:
                self.history.fail(
                    "server",
                    "runner-state",
                    "runner_status_unavailable",
                    details={"exception_type": self._runner_error},
                )
                continue
            self.history.recover("server", "runner-state")
            for runner in self._runners:
                age = max(0, time.time() - runner["seen"])
                details = {
                    "last_seen": runner["seen"],
                    "age_seconds": age,
                    "threshold_seconds": 15,
                    "observed_at": self._runners_observed_at,
                }
                if age >= 15:
                    self.history.fail(
                        "runner",
                        runner["id"],
                        "runner_heartbeat_expired",
                        runner_id=runner["id"],
                        details=details,
                    )
                else:
                    self.history.recover(
                        "runner", runner["id"], runner_id=runner["id"], details=details
                    )

    def close(self):
        if self._timer is not None:
            self._timer.cancel()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
