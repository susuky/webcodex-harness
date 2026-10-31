from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from contextlib import contextmanager

from .store import Store, new_id

APP_URI = "ui://webcodex/task-progress/v1"
APP_MIME = "text/html;profile=mcp-app"
LEASE_SECONDS = 120
COALESCE_SECONDS = 2
ACTIVE = ("watching", "ready", "prepared", "accepted", "unknown")


def fingerprint(value: str) -> str:
    return hashlib.sha256(("webcodex-task-view-v1\0" + value).encode()).hexdigest()


class Continuations:
    """One durable wait and one leased presentation controller per existing task."""

    def __init__(self, store: Store):
        self.store, self.db = store, store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS task_controllers (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                generation INTEGER NOT NULL DEFAULT 0,
                window_hash TEXT, view_hash TEXT,
                lease_until REAL NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS task_views (
                task_id TEXT NOT NULL REFERENCES tasks(id), view_hash TEXT NOT NULL,
                retired INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(task_id, view_hash)
            );
            CREATE TABLE IF NOT EXISTS task_waits (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                request_id TEXT NOT NULL, original_jobs TEXT NOT NULL, jobs TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'watching','ready','prepared','accepted','unknown','consumed','cancelled'
                )),
                generation INTEGER, view_hash TEXT, consume_hash TEXT,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(task_id, request_id)
            );
            CREATE INDEX IF NOT EXISTS waits_by_task ON task_waits(task_id, created DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_wait ON task_waits(task_id)
                WHERE state IN ('watching','ready','prepared','accepted','unknown');
            CREATE TABLE IF NOT EXISTS task_message_policy (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                max_auto_messages INTEGER NOT NULL DEFAULT 3,
                min_interval_seconds INTEGER NOT NULL DEFAULT 60
            );
            CREATE TABLE IF NOT EXISTS task_deliveries (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                wait_id TEXT NOT NULL UNIQUE REFERENCES task_waits(id),
                request_id TEXT, mode TEXT NOT NULL, reason TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, sent_at REAL,
                UNIQUE(task_id,request_id)
            );
            CREATE INDEX IF NOT EXISTS task_delivery_history ON task_deliveries(task_id,created);
            CREATE TABLE IF NOT EXISTS task_watch_members (
                task_id TEXT NOT NULL REFERENCES tasks(id), job_id TEXT NOT NULL,
                wait_id TEXT NOT NULL REFERENCES task_waits(id),
                PRIMARY KEY(task_id,job_id)
            );
            CREATE TABLE IF NOT EXISTS task_handoffs (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                snapshot TEXT NOT NULL, updated REAL NOT NULL
            );
        """)
        with self.db:
            for table, column, declaration in (
                ("task_controllers", "auto_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("task_controllers", "enabled_at", "REAL NOT NULL DEFAULT 0"),
                ("task_controllers", "updated_at", "REAL NOT NULL DEFAULT 0"),
                ("task_waits", "ready_at", "REAL"),
            ):
                if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            # Consent belongs to a live App connection and is never restored on Server startup.
            self.db.execute("UPDATE task_controllers SET auto_enabled=0")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def current(self, task_id: str) -> dict | None:
        return self.store.one(
            "SELECT * FROM task_waits WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
        )

    def connected(self, task_id: str, window: str | None) -> bool:
        controller = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task_id,))
        return bool(
            window is not None
            and controller
            and controller["lease_until"] > time.time()
            and controller["window_hash"] == window
        )

    def arm(
        self, task_id: str, job_ids: list[str], request_id: str, replace_wait_id: str | None = None
    ) -> dict:
        encoded = json.dumps(sorted(set(job_ids)))
        now = time.time()
        with self.transaction():
            previous = self.store.one(
                "SELECT * FROM task_waits WHERE task_id=? AND request_id=?", (task_id, request_id)
            )
            if previous:
                if previous["original_jobs"] != encoded:
                    raise ValueError("This wait request_id already names different jobs")
                return previous
            active = self.store.one(
                "SELECT * FROM task_waits WHERE task_id=? "
                "AND state IN ('watching','ready','prepared','accepted','unknown')",
                (task_id,),
            )
            if active:
                if active["id"] != replace_wait_id or active["state"] not in {
                    "prepared",
                    "accepted",
                    "unknown",
                }:
                    raise ValueError("This task already has an unconsumed wait; inspect task_get")
                self.db.execute(
                    "UPDATE task_waits SET state='cancelled',updated=? WHERE id=?",
                    (now, active["id"]),
                )
                self.store.event(task_id, "continuation.replaced", {"wait_id": active["id"]})
            elif replace_wait_id:
                raise ValueError("The wait to replace is no longer active; inspect task_get")
            wait_id = new_id()
            self.db.execute(
                "INSERT INTO task_waits(id,task_id,request_id,original_jobs,jobs,state,created,updated) "
                "VALUES (?,?,?,?,?,'watching',?,?)",
                (wait_id, task_id, request_id, encoded, encoded, now, now),
            )
            self.store.event(task_id, "continuation.armed", {"wait_id": wait_id})
        return self.current(task_id)

    def refresh(self, wait_id: str, prior_jobs: str, jobs: list[str], ready: bool):
        with self.transaction():
            self.db.execute(
                "UPDATE task_waits SET jobs=?,state=?,updated=?,ready_at=? "
                "WHERE id=? AND state='watching' AND jobs=?",
                (
                    json.dumps(jobs),
                    "ready" if ready else "watching",
                    time.time(),
                    time.time() if ready else None,
                    wait_id,
                    prior_jobs,
                ),
            )

    def _lose_view(self, task_id: str, now: float):
        # A prepared Host call may already have happened. Never make it sendable again.
        self.db.execute(
            "UPDATE task_waits SET state='unknown',updated=? "
            "WHERE task_id=? AND state IN ('prepared','accepted')",
            (now, task_id),
        )
        self.db.execute(
            "UPDATE task_deliveries SET status='unknown',updated=? "
            "WHERE task_id=? AND status IN ('prepared','accepted')",
            (now, task_id),
        )

    def bind(self, task_id: str, view_id: str, window: str | None) -> dict:
        if not window:
            return {"bound": False, "reason": "missing_window"}
        now, view = time.time(), fingerprint(view_id)
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO task_controllers(task_id) VALUES (?)", (task_id,)
            )
            current = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task_id,))
            known = self.store.one(
                "SELECT retired FROM task_views WHERE task_id=? AND view_hash=?", (task_id, view)
            )
            same_view = current["view_hash"] == view
            same_window = window is not None and current["window_hash"] == window
            recovering = bool(known and known["retired"])
            if recovering and (not same_window or current["lease_until"] > now):
                return {"bound": False, "reason": "replaced_view"}
            if not same_view and current["lease_until"] > now and not same_window:
                return {"bound": False, "reason": "another_view"}
            if not same_view or current["lease_until"] <= now:
                self._lose_view(task_id, now)
                current["generation"] += 1
            if not same_view and current["view_hash"]:
                self.db.execute(
                    "UPDATE task_views SET retired=1 WHERE task_id=? AND view_hash=?",
                    (task_id, current["view_hash"]),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO task_views(task_id,view_hash) VALUES (?,?)", (task_id, view)
            )
            if recovering:
                self.db.execute(
                    "UPDATE task_views SET retired=0 WHERE task_id=? AND view_hash=?",
                    (task_id, view),
                )
            self.db.execute(
                "UPDATE task_controllers SET generation=?,window_hash=?,view_hash=?,lease_until=?,"
                "auto_enabled=0,paused=0,enabled_at=0,updated_at=? "
                "WHERE task_id=?",
                (current["generation"], window, view, now + LEASE_SECONDS, now, task_id),
            )
            if recovering:
                self.store.event(
                    task_id, "continuation.view_recovered", {"generation": current["generation"]}
                )
        return {"bound": True, "generation": current["generation"]}

    def takeover(self, task_id: str, view_id: str, window: str | None) -> dict:
        if not window:
            raise ValueError("A conversation identity is required to take control")
        now, view = time.time(), fingerprint(view_id)
        with self.transaction():
            current = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task_id,))
            generation = (current["generation"] if current else 0) + 1
            self._lose_view(task_id, now)
            self.db.execute("UPDATE task_views SET retired=1 WHERE task_id=?", (task_id,))
            self.db.execute(
                "INSERT INTO task_views(task_id,view_hash,retired) VALUES (?,?,0) "
                "ON CONFLICT(task_id,view_hash) DO UPDATE SET retired=0",
                (task_id, view),
            )
            self.db.execute(
                "INSERT INTO task_controllers(task_id,generation,window_hash,view_hash,lease_until,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "generation=excluded.generation,window_hash=excluded.window_hash,view_hash=excluded.view_hash,"
                "lease_until=excluded.lease_until,updated_at=excluded.updated_at,auto_enabled=0,paused=0,enabled_at=0",
                (task_id, generation, window, view, now + LEASE_SECONDS, now),
            )
            wait = self.current(task_id)
            if wait and wait["state"] in {"prepared", "accepted", "unknown"}:
                self.db.execute(
                    "UPDATE task_waits SET state='cancelled',consume_hash=NULL,updated=? WHERE id=?",
                    (now, wait["id"]),
                )
                self.db.execute(
                    "INSERT INTO task_waits(id,task_id,request_id,original_jobs,jobs,state,created,updated,ready_at) "
                    "VALUES (?,?,?,?,?,'ready',?,?,?)",
                    (
                        new_id(),
                        task_id,
                        f"takeover:{generation}",
                        wait["original_jobs"],
                        wait["jobs"],
                        now,
                        now,
                        now,
                    ),
                )
            self.store.event(task_id, "continuation.takeover", {"generation": generation})
        return {"bound": True, "generation": generation}

    def policy(self, task_id: str) -> dict:
        settings = self.store.one(
            "SELECT * FROM task_message_policy WHERE task_id=?", (task_id,)
        ) or {
            "max_auto_messages": 3,
            "min_interval_seconds": 60,
        }
        counts = self.store.one(
            "SELECT count(*) AS count,max(coalesce(sent_at,created)) AS last FROM task_deliveries "
            "WHERE task_id=? AND mode='auto' AND status!='not_sent'",
            (task_id,),
        )
        return {
            "max_auto_messages": settings["max_auto_messages"],
            "min_interval_seconds": settings["min_interval_seconds"],
            "auto_message_count": counts["count"],
            "last_auto_at": counts["last"],
            "next_auto_at": counts["last"] + settings["min_interval_seconds"]
            if counts["last"]
            else None,
        }

    def auto_blocked_reason(
        self, task_id: str, controller: dict | None, wait: dict | None
    ) -> str | None:
        now = time.time()
        if not controller or not controller["auto_enabled"] or controller["lease_until"] <= now:
            return "disabled"
        policy = self.policy(task_id)
        if policy["auto_message_count"] >= policy["max_auto_messages"]:
            return "limit"
        if policy["next_auto_at"] and policy["next_auto_at"] > now:
            return "interval"
        if not wait or wait["state"] != "ready":
            return "waiting"
        if now - (wait.get("ready_at") or wait["updated"]) < COALESCE_SECONDS:
            return "batching"
        return None

    def append_jobs(self, task_id: str, wait_id: str, job_ids: list[str]):
        with self.transaction():
            wait = self.current(task_id)
            if not wait or wait["id"] != wait_id or wait["state"] not in {"watching", "ready"}:
                return
            known = {
                r["job_id"]
                for r in self.store.rows(
                    "SELECT job_id FROM task_watch_members WHERE task_id=?", (task_id,)
                )
            }
            added = sorted(set(job_ids) - known)
            if not added:
                return
            jobs = sorted(set(json.loads(wait["jobs"])) | set(added))
            self.db.execute(
                "UPDATE task_waits SET jobs=?,state='watching',ready_at=NULL,updated=? WHERE id=?",
                (json.dumps(jobs), time.time(), wait_id),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO task_watch_members(task_id,job_id,wait_id) VALUES (?,?,?)",
                [(task_id, job_id, wait_id) for job_id in added],
            )

    def controller(self, task_id: str, view_id: str, generation: int, window: str | None) -> dict:
        current = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task_id,))
        if (
            not current
            or current["generation"] != generation
            or not hmac.compare_digest(current["view_hash"] or "", fingerprint(view_id))
            or current["window_hash"] != window
            or current["lease_until"] <= time.time()
        ):
            raise ValueError("This task view is no longer the active controller; bind again")
        return current

    def action(
        self,
        task_id: str,
        view_id: str,
        generation: int,
        window: str | None,
        action: str,
        wait_id: str | None = None,
        outcome: str | None = None,
        *,
        max_auto_messages: int | None = None,
        min_interval_seconds: int | None = None,
        request_id: str | None = None,
    ) -> dict:
        now = time.time()
        with self.transaction():
            controller = self.controller(task_id, view_id, generation, window)
            self.db.execute(
                "UPDATE task_controllers SET lease_until=?,updated_at=? WHERE task_id=?",
                (now + LEASE_SECONDS, now, task_id),
            )
            wait = self.current(task_id)
            if action in {"pause", "enable"}:
                self.db.execute(
                    "UPDATE task_controllers SET paused=?,auto_enabled=?,enabled_at=? WHERE task_id=?",
                    (
                        action == "pause",
                        action == "enable",
                        now if action == "enable" else 0,
                        task_id,
                    ),
                )
                return {"paused": action == "pause", "auto_enabled": action == "enable"}
            if action == "configure":
                policy = self.policy(task_id)
                maximum = (
                    policy["max_auto_messages"] if max_auto_messages is None else max_auto_messages
                )
                interval = (
                    policy["min_interval_seconds"]
                    if min_interval_seconds is None
                    else min_interval_seconds
                )
                if (
                    isinstance(maximum, bool)
                    or not 0 <= maximum <= 20
                    or isinstance(interval, bool)
                    or not 10 <= interval <= 86400
                ):
                    raise ValueError(
                        "Use 0–20 automatic messages and an interval of 10–86400 seconds"
                    )
                self.db.execute(
                    "INSERT INTO task_message_policy(task_id,max_auto_messages,min_interval_seconds) VALUES (?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET max_auto_messages=excluded.max_auto_messages,"
                    "min_interval_seconds=excluded.min_interval_seconds",
                    (task_id, maximum, interval),
                )
                return {"policy": self.policy(task_id)}
            if action == "unbind":
                self._lose_view(task_id, now)
                self.db.execute(
                    "UPDATE task_controllers SET lease_until=0,auto_enabled=0 WHERE task_id=?",
                    (task_id,),
                )
                return {"bound": False}
            if action == "state":
                return {
                    "bound": True,
                    "generation": generation,
                    "paused": bool(controller["paused"]),
                    "auto_enabled": bool(controller["auto_enabled"]),
                }
            if not wait or (wait_id is not None and wait["id"] != wait_id):
                raise ValueError("This wait is no longer current")
            wait_id = wait["id"]
            if action in {"prepare", "prepare_manual"}:
                previous = self.store.one(
                    "SELECT id,status FROM task_deliveries WHERE task_id=? AND (wait_id=? OR request_id=?)",
                    (task_id, wait_id, request_id),
                )
                if previous:
                    return {"wait_id": wait_id, "duplicate": True, "state": wait["state"]}
                if action == "prepare":
                    reason = self.auto_blocked_reason(task_id, controller, wait)
                    if reason:
                        raise ValueError(
                            f"This wait is not available for automatic continuation: {reason}"
                        )
                elif wait["state"] not in {"watching", "ready"}:
                    raise ValueError("This wait is not available for manual continuation")
                token = secrets.token_urlsafe(32)
                self.db.execute(
                    "UPDATE task_waits SET state='prepared',generation=?,view_hash=?,consume_hash=?,updated=? "
                    "WHERE id=?",
                    (generation, fingerprint(view_id), fingerprint(token), now, wait_id),
                )
                mode = "auto" if action == "prepare" else "manual"
                message = (
                    "請接續 WebCodex 任務。先呼叫 task_resume，參數為 "
                    + json.dumps({"task_id": task_id, "wait_id": wait_id, "consume_token": token})
                    + "。讀取交接資訊與未讀輸出，從現有工作結果繼續。"
                )
                self.db.execute(
                    "INSERT INTO task_deliveries(id,task_id,wait_id,request_id,mode,reason,content,status,created,updated) "
                    "VALUES (?,?,?,?,?,?,?,'prepared',?,?)",
                    (
                        new_id(),
                        task_id,
                        wait_id,
                        request_id,
                        mode,
                        "jobs_completed" if mode == "auto" else "user_resume",
                        message.replace(token, "[REDACTED]"),
                        now,
                        now,
                    ),
                )
                self.store.event(
                    task_id, "continuation.prepared", {"wait_id": wait_id, "mode": mode}
                )
                return {
                    "wait_id": wait_id,
                    "state": "prepared",
                    "app_protocol": {
                        "message": message,
                    },
                }
            if action == "finish":
                if wait["generation"] != generation or wait["view_hash"] != fingerprint(view_id):
                    raise ValueError("The continuation belongs to another view")
                if outcome not in {"accepted", "unknown", "not_sent"}:
                    raise ValueError("Invalid continuation delivery outcome")
                if outcome == "not_sent":
                    if wait["state"] != "prepared":
                        raise ValueError(
                            "Only a prepared continuation can be withdrawn before sending"
                        )
                    self.db.execute(
                        "UPDATE task_waits SET state='cancelled',consume_hash=NULL,updated=? WHERE id=?",
                        (now, wait_id),
                    )
                    self.db.execute(
                        "UPDATE task_deliveries SET status='not_sent',updated=? WHERE wait_id=?",
                        (now, wait_id),
                    )
                    # A fresh wait fences late finish retries and the withdrawn consume token.
                    next_wait = new_id()
                    self.db.execute(
                        "INSERT INTO task_waits(id,task_id,request_id,original_jobs,jobs,state,created,updated) "
                        "VALUES (?,?,?,?,?,'watching',?,?)",
                        (
                            next_wait,
                            task_id,
                            f"not-sent:{wait_id}",
                            wait["original_jobs"],
                            wait["jobs"],
                            now,
                            now,
                        ),
                    )
                    self.store.event(task_id, "continuation.not_sent", {"wait_id": wait_id})
                    return {"wait_id": wait_id, "state": "not_sent"}
                if wait["state"] not in {"prepared", "accepted", "unknown", "consumed"}:
                    raise ValueError("The continuation was not prepared")
                if wait["state"] != "consumed":
                    # Unknown cannot later be weakened to accepted by a late acknowledgement.
                    state = "unknown" if wait["state"] == "unknown" else outcome
                    self.db.execute(
                        "UPDATE task_waits SET state=?,updated=? WHERE id=?", (state, now, wait_id)
                    )
                self.db.execute(
                    "UPDATE task_deliveries SET status=?,updated=?,sent_at=coalesce(sent_at,?) WHERE wait_id=?",
                    (self.current(task_id)["state"], now, now, wait_id),
                )
                return {"wait_id": wait_id, "state": self.current(task_id)["state"]}
            raise ValueError("Unknown task App action")

    def consume(self, task_id: str, wait_id: str, token: str) -> dict:
        with self.transaction():
            wait = self.current(task_id)
            if (
                not wait
                or wait["id"] != wait_id
                or wait["state"] not in {"prepared", "accepted", "unknown", "consumed"}
                or not hmac.compare_digest(wait["consume_hash"] or "", fingerprint(token))
            ):
                raise ValueError("This continuation is invalid or no longer current")
            duplicate = wait["state"] == "consumed"
            if not duplicate:
                self.db.execute(
                    "UPDATE task_waits SET state='consumed',updated=? WHERE id=?",
                    (time.time(), wait_id),
                )
                self.store.event(task_id, "continuation.consumed", {"wait_id": wait_id})
                self.db.execute(
                    "UPDATE task_deliveries SET status='consumed',updated=?,sent_at=coalesce(sent_at,?) WHERE wait_id=?",
                    (time.time(), time.time(), wait_id),
                )
        return {"wait_id": wait_id, "consumed": True, "already_consumed": duplicate}

    def cancel(self, task_id: str):
        with self.transaction():
            self._lose_view(task_id, time.time())
            self.db.execute(
                "UPDATE task_controllers SET auto_enabled=0 WHERE task_id=?", (task_id,)
            )
            self.db.execute(
                "UPDATE task_waits SET state='cancelled',updated=? WHERE task_id=? "
                "AND state IN ('watching','ready','prepared','accepted','unknown')",
                (time.time(), task_id),
            )

    def public(self, task_id: str, window: str | None = None) -> dict:
        wait = self.current(task_id)
        controller = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task_id,))
        live = bool(controller and controller["lease_until"] > time.time())
        state = (
            "unbound"
            if not controller
            else "expired"
            if not live
            else "owned"
            if controller["window_hash"] == window
            else "other"
        )
        policy = self.policy(task_id)
        policy["auto_blocked_reason"] = self.auto_blocked_reason(task_id, controller, wait)
        return {
            "paused": bool(controller and controller["paused"]),
            "auto_enabled": bool(live and controller["auto_enabled"]),
            "wait": {"id": wait["id"], "state": wait["state"]} if wait else None,
            "policy": policy,
            "controller": {
                "state": state,
                "updated_at": controller["updated_at"] if controller else None,
                "lease_until": controller["lease_until"] if controller else None,
                "generation": controller["generation"] if controller else None,
            },
            "deliveries": self.store.rows(
                "SELECT id,wait_id,mode,reason,content,status,created,updated,sent_at FROM task_deliveries "
                "WHERE task_id=? ORDER BY created DESC LIMIT 50",
                (task_id,),
            ),
        }
