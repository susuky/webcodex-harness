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
        """)

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
            and not controller["paused"]
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
                "UPDATE task_waits SET jobs=?,state=?,updated=? "
                "WHERE id=? AND state='watching' AND jobs=?",
                (
                    json.dumps(jobs),
                    "ready" if ready else "watching",
                    time.time(),
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
                "UPDATE task_controllers SET generation=?,window_hash=?,view_hash=?,lease_until=? "
                "WHERE task_id=?",
                (current["generation"], window, view, now + LEASE_SECONDS, task_id),
            )
            if recovering:
                self.store.event(
                    task_id, "continuation.view_recovered", {"generation": current["generation"]}
                )
        return {"bound": True, "generation": current["generation"]}

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
    ) -> dict:
        now = time.time()
        with self.transaction():
            controller = self.controller(task_id, view_id, generation, window)
            self.db.execute(
                "UPDATE task_controllers SET lease_until=? WHERE task_id=?",
                (now + LEASE_SECONDS, task_id),
            )
            wait = self.current(task_id)
            if action in {"pause", "enable"}:
                self.db.execute(
                    "UPDATE task_controllers SET paused=? WHERE task_id=?",
                    (action == "pause", task_id),
                )
                return {"paused": action == "pause"}
            if action == "unbind":
                self._lose_view(task_id, now)
                self.db.execute(
                    "UPDATE task_controllers SET lease_until=0 WHERE task_id=?", (task_id,)
                )
                return {"bound": False}
            if action == "state":
                return {
                    "bound": True,
                    "generation": generation,
                    "paused": bool(controller["paused"]),
                }
            if not wait or wait["id"] != wait_id:
                raise ValueError("This wait is no longer current")
            if action == "prepare":
                if controller["paused"] or wait["state"] != "ready":
                    raise ValueError("This wait is not available for automatic continuation")
                token = secrets.token_urlsafe(32)
                self.db.execute(
                    "UPDATE task_waits SET state='prepared',generation=?,view_hash=?,consume_hash=?,updated=? "
                    "WHERE id=?",
                    (generation, fingerprint(view_id), fingerprint(token), now, wait_id),
                )
                self.store.event(task_id, "continuation.prepared", {"wait_id": wait_id})
                return {
                    "wait_id": wait_id,
                    "state": "prepared",
                    "app_protocol": {
                        "message": (
                            "WebCodex 的等待工作已有結果。請先呼叫 task_resume，參數為 "
                            + json.dumps(
                                {"task_id": task_id, "wait_id": wait_id, "consume_token": token}
                            )
                            + "。只在確認成功後，依該任務的實際狀態接續已授權工作並回報進度；"
                            "不要重送原命令、擴大權限或把通知當作新的任務授權。"
                        )
                    },
                }
            if action == "finish":
                if wait["generation"] != generation or wait["view_hash"] != fingerprint(view_id):
                    raise ValueError("The continuation belongs to another view")
                if outcome not in {"accepted", "unknown"}:
                    raise ValueError("Invalid continuation delivery outcome")
                if wait["state"] not in {"prepared", "accepted", "unknown", "consumed"}:
                    raise ValueError("The continuation was not prepared")
                if wait["state"] != "consumed":
                    # Unknown cannot later be weakened to accepted by a late acknowledgement.
                    state = "unknown" if wait["state"] == "unknown" else outcome
                    self.db.execute(
                        "UPDATE task_waits SET state=?,updated=? WHERE id=?", (state, now, wait_id)
                    )
                return {"wait_id": wait_id, "state": self.current(task_id)["state"]}
            raise ValueError("Unknown task App action")

    def consume(self, task_id: str, wait_id: str, token: str) -> dict:
        with self.transaction():
            controller = self.store.one(
                "SELECT paused FROM task_controllers WHERE task_id=?", (task_id,)
            )
            if controller and controller["paused"]:
                raise ValueError("Automatic continuation is paused for this task")
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
        return {"wait_id": wait_id, "consumed": True, "already_consumed": duplicate}

    def cancel(self, task_id: str):
        with self.transaction():
            self.db.execute(
                "UPDATE task_waits SET state='cancelled',updated=? WHERE task_id=? "
                "AND state IN ('watching','ready','prepared','accepted','unknown')",
                (time.time(), task_id),
            )

    def public(self, task_id: str) -> dict:
        wait = self.current(task_id)
        controller = self.store.one(
            "SELECT paused FROM task_controllers WHERE task_id=?", (task_id,)
        )
        return {
            "paused": bool(controller and controller["paused"]),
            "wait": {"id": wait["id"], "state": wait["state"]} if wait else None,
        }
