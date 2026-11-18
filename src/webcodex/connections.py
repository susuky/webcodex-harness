"""Private, bounded connection incidents shared by request and watchdog threads."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .config import private_dir

REASONS = {
    "source_connect_timeout": "連線至來源逾時",
    "source_read_timeout": "來源回應逾時",
    "source_timeout": "來源通訊逾時",
    "source_unreachable": "無法連線至來源",
    "source_auth_failed": "來源驗證失敗或權限已撤銷",
    "source_http_error": "來源回應錯誤",
    "source_invalid_response": "來源回傳了無效資料",
    "event_loop_blocked": "伺服器回應停頓",
    "runner_heartbeat_expired": "執行環境心跳逾期",
    "runner_status_unavailable": "讀取執行環境狀態失敗",
    "monitor_scheduling_gap": "監測排程中斷",
}
_NUMBERS = {
    "http_status",
    "duration_ms",
    "timeout_seconds",
    "last_seen",
    "last_seen_at",
    "heartbeat_age_seconds",
    "heartbeat_limit_seconds",
    "age_seconds",
    "threshold_seconds",
    "pending_results",
    "event_loop_delay_ms",
    "query_duration_ms",
    "blocked_seconds",
    "observed_at",
    "previous_observed_at",
    "sampled_at",
    "gap_seconds",
    "slowest_ms",
    "requests",
    "pid",
}
_CODES = {"exception_type", "method", "phase", "operation", "table", "tool"}
_PRIVATE_DETAILS = {"query", "stack", "location"}
_DIAGNOSTIC_DETAILS = {
    "exception_type",
    "http_status",
    "method",
    "route",
    "phase",
    "operation",
    "table",
    "tool",
    "query",
    "location",
}


def safe_details(details: dict | None) -> dict:
    result = {}
    for name, value in (details or {}).items():
        if name in _NUMBERS and type(value) in (int, float) and math.isfinite(value):
            result[name] = value
        elif (
            name in _CODES
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
        ):
            result[name] = value
        elif (
            name == "route"
            and isinstance(value, str)
            and re.fullmatch(r"/[A-Za-z0-9_/:.-]{0,180}", value)
        ):
            result[name] = value
        elif name == "query" and isinstance(value, str):
            # Callers supply only parameterized SQL templates, never bound values.
            result[name] = " ".join(value.split())[:2000]
        elif name == "location" and isinstance(value, str):
            result[name] = value[:200]
        elif name == "stack" and isinstance(value, list):
            result[name] = [entry[:200] for entry in value[:20] if isinstance(entry, str)]
    return result


def public_details(details: dict | None) -> dict:
    return {
        name: value for name, value in safe_details(details).items() if name not in _PRIVATE_DETAILS
    }


class ConnectionHistory:
    def __init__(self, directory: Path, limit: int = 100):
        self.directory = private_dir(directory)
        self.path = self.directory / "events.jsonl"
        self.limit = limit
        self.lock = threading.RLock()
        self.incidents = []
        self.persisted = True
        self.recording_error = None
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                self.incidents = [json.loads(line) for line in stream if line.strip()]

    @staticmethod
    def _identity(incident):
        return tuple(
            incident[name] for name in ("component", "key", "source_id", "runner_id", "owner")
        )

    @staticmethod
    def _active(incident):
        return incident["recovered_at"] is None and incident.get("closed_at") is None

    def _save(self):
        # Keep outstanding incidents even when the recent recovered history is full.
        closed = sorted(
            (row for row in self.incidents if not self._active(row)),
            key=lambda row: row.get("closed_at") or row["recovered_at"],
        )
        keep = {row["id"] for row in closed[-self.limit :]}
        self.incidents = [row for row in self.incidents if self._active(row) or row["id"] in keep]
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix="events-", suffix=".tmp", dir=self.directory
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for row in self.incidents:
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            os.replace(temporary, self.path)
            self.persisted, self.recording_error = True, None
        except OSError as error:
            self.persisted, self.recording_error = False, type(error).__name__
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def fail(
        self, component, key, reason, *, source_id="local", runner_id=None, owner=None, details=None
    ):
        if reason not in REASONS:
            raise ValueError("Unknown connection failure reason")
        identity = (component, key, source_id, runner_id, owner)
        details = safe_details(details)
        with self.lock:
            now = time.time()
            for incident in reversed(self.incidents):
                if self._identity(incident) == identity and self._active(incident):
                    incident["last_seen_at"] = now
                    changed = reason != incident["reason"] or any(
                        incident["details"].get(name) != value
                        for name, value in details.items()
                        if name in _DIAGNOSTIC_DETAILS
                    )
                    if not changed:
                        return incident["id"]
                    incident.update(closed_at=now, end_reason="diagnostic_changed")
                    break
            incident = {
                "id": uuid.uuid4().hex,
                "component": component,
                "key": key,
                "source_id": source_id,
                "runner_id": runner_id,
                "owner": owner,
                "started_at": now,
                "last_seen_at": now,
                "recovered_at": None,
                "reason": reason,
                "details": details,
            }
            self.incidents.append(incident)
            self._save()
            return incident["id"]

    def recover(
        self, component, key, *, source_id="local", runner_id=None, owner=None, details=None
    ):
        identity = (component, key, source_id, runner_id, owner)
        with self.lock:
            for incident in reversed(self.incidents):
                if self._identity(incident) == identity and self._active(incident):
                    incident["recovered_at"] = time.time()
                    incident["last_seen_at"] = incident["recovered_at"]
                    if details:
                        incident["recovery_details"] = safe_details(details)
                    self._save()
                    return incident["id"]
        return None

    def snapshot(self, *, owner, source_ids, runner_ids=None, limit=30, include_evidence=False):
        with self.lock:
            visible = [
                row
                for row in self.incidents
                if row["owner"] in (None, owner)
                and row["source_id"] in source_ids
                and not (
                    row["source_id"] == "local"
                    and row["runner_id"] is not None
                    and runner_ids is not None
                    and row["runner_id"] not in runner_ids
                )
            ]
            visible.sort(
                key=lambda row: (
                    row["recovered_at"] or row.get("closed_at") or row["started_at"],
                    self._active(row),
                ),
                reverse=True,
            )
            incidents = []
            for row in visible[:limit]:
                result = copy.deepcopy(
                    {name: value for name, value in row.items() if name not in {"key", "owner"}}
                )
                for field in ("details", "recovery_details"):
                    if field in result:
                        result[field] = (
                            safe_details(result[field])
                            if include_evidence
                            else public_details(result[field])
                        )
                result["state"] = (
                    "changed"
                    if result.get("closed_at") is not None
                    else "active"
                    if result["recovered_at"] is None
                    else "recovered"
                )
                result["reason_label"] = REASONS[result["reason"]]
                incidents.append(result)
            return {
                "observed_at": time.time(),
                "incidents": incidents,
                "persisted": self.persisted,
                "recording_error": self.recording_error,
            }
