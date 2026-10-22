from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
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
} | {"progress.error"}
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
                elif key in {"is_error", "disconnected", "control_bound"} and type(value) is bool:
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
