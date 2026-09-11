from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .config import private_dir


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, directory: Path):
        self.db = sqlite3.connect(private_dir(directory) / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS runners (
                id TEXT PRIMARY KEY, instance TEXT NOT NULL,
                projects TEXT NOT NULL, seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, runner TEXT NOT NULL,
                project TEXT NOT NULL, binding TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', summary TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                owner TEXT NOT NULL, runner TEXT NOT NULL, project TEXT NOT NULL,
                binding TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL,
                status TEXT NOT NULL, instance TEXT, result TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                request_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner, task_id, request_id)
            );
            CREATE INDEX IF NOT EXISTS queue ON jobs(runner, status, created);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_events ON events(task_id, id);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                question TEXT NOT NULL, answer TEXT, created REAL NOT NULL
            );
        """)

    def one(self, sql, params=()) -> dict | None:
        row = self.db.execute(sql, params).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ["projects", "plan", "arguments", "result", "data"]:
            if key in data and data[key] is not None:
                data[key] = json.loads(data[key])
        return data

    def rows(self, sql, params=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, params)]

    def event(self, task: str, kind: str, data: dict):
        self.db.execute(
            "INSERT INTO events(task_id,kind,data,created) VALUES (?,?,?,?)",
            (task, kind, json.dumps(data, ensure_ascii=False), time.time()),
        )

    def register(
        self, runner: str, instance: str, projects: dict, known_jobs: list[str] | None = None
    ):
        now = time.time()
        previous = self.one("SELECT * FROM runners WHERE id=?", (runner,))
        if previous and previous["instance"] != instance and now - previous["seen"] < 15:
            raise ValueError(
                "Another Runner instance is still connected; retry after its lease expires"
            )
        with self.db:
            if previous:
                for job in self.rows(
                    "SELECT id, task_id FROM jobs WHERE runner=? AND status='running'", (runner,)
                ):
                    if previous["instance"] == instance and job["id"] in (known_jobs or []):
                        continue
                    self.db.execute(
                        "UPDATE jobs SET status='unknown', updated=? WHERE id=?",
                        (now, job["id"]),
                    )
                    self.event(job["task_id"], "job.unknown", {"job_id": job["id"]})
            self.db.execute(
                "INSERT INTO runners VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "instance=excluded.instance,projects=excluded.projects,seen=excluded.seen",
                (runner, instance, json.dumps(projects), now),
            )

    def inventory(self) -> list[dict]:
        result = []
        for row in self.rows("SELECT * FROM runners ORDER BY id"):
            for project, binding in json.loads(row["projects"]).items():
                result.append(
                    {
                        "id": row["id"] + "/" + project,
                        "online": time.time() - row["seen"] < 15,
                        "binding": binding,
                    }
                )
        return result

    def create_task(self, owner: str, project: str, title: str) -> dict:
        runner, name = project.split("/", 1)
        row = self.one("SELECT * FROM runners WHERE id=?", (runner,))
        if not row or name not in row["projects"]:
            raise ValueError("Project is not registered; start its Runner first")
        task, now = new_id(), time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,owner,runner,project,binding,title,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task, owner, runner, name, row["projects"][name], title, now, now),
            )
            self.event(task, "task.created", {"title": title})
        return self.task(task, owner)

    def task(self, task_id: str, owner: str) -> dict:
        row = self.one("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, owner))
        if row is None:
            raise ValueError("Task not found")
        return row

    def update_task(self, task_id: str, owner: str, status: str, summary: str, plan: list) -> dict:
        self.task(task_id, owner)
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status=?, summary=?, plan=?, updated=? WHERE id=?",
                (status, summary, json.dumps(plan), time.time(), task_id),
            )
            self.event(task_id, "task.updated", {"status": status})
        return self.task(task_id, owner)

    def enqueue(
        self,
        task: dict,
        tool: str,
        arguments: dict,
        request_id: str | None,
        approval: bool,
        limit: int,
    ) -> dict:
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        if request_id:
            old = self.one(
                "SELECT * FROM jobs WHERE owner=? AND task_id=? AND request_id=?",
                (task["owner"], task["id"], request_id),
            )
            if old:
                if old["tool"] != tool or old["arguments"] != arguments:
                    raise ValueError("request_id already belongs to a different call")
                return self.job(old["id"], task["owner"])
        if task["status"] == "completed":
            raise ValueError("Task is completed; use task_update to reopen it first")
        runner = self.one("SELECT * FROM runners WHERE id=?", (task["runner"],))
        if not runner or runner["projects"].get(task["project"]) != task["binding"]:
            raise ValueError("Project root changed; create a new task for the new root")
        count = self.db.execute(
            "SELECT count(*) FROM jobs WHERE owner=? AND status IN ('queued','pending_approval','running')",
            (task["owner"],),
        ).fetchone()[0]
        if count >= limit:
            raise ValueError("Too many unfinished jobs; finish or cancel existing jobs first")
        job, now = new_id(), time.time()
        status = "pending_approval" if approval else "queued"
        with self.db:
            self.db.execute(
                "INSERT INTO jobs(id,task_id,owner,runner,project,binding,tool,arguments,status,"
                "request_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job,
                    task["id"],
                    task["owner"],
                    task["runner"],
                    task["project"],
                    task["binding"],
                    tool,
                    encoded,
                    status,
                    request_id,
                    now,
                    now,
                ),
            )
            self.event(task["id"], "job." + status, {"job_id": job, "tool": tool})
        return self.job(job, task["owner"])

    def job(self, job_id: str, owner: str) -> dict:
        row = self.one("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner))
        if row is None:
            raise ValueError("Job not found")
        for key in ["arguments", "instance", "binding"]:
            row.pop(key)
        row["job_id"] = row.pop("id")
        if row["status"] == "pending_approval":
            row["next_step"] = (
                "The operator must approve this job with the local CLI. Then poll job_get."
            )
        elif row["status"] == "unknown":
            row["next_step"] = (
                "Execution outcome is uncertain. Inspect repo/process state before starting a replacement."
            )
        return row

    def claim(self, runner_id: str, instance: str, limit: int) -> list[dict]:
        runner = self.one("SELECT * FROM runners WHERE id=?", (runner_id,))
        if not runner or runner["instance"] != instance:
            raise ValueError("Runner must register this instance before polling")
        result = []
        with self.db:
            self.db.execute("UPDATE runners SET seen=? WHERE id=?", (time.time(), runner_id))
            for row in self.rows(
                "SELECT id FROM jobs WHERE runner=? AND status='queued' ORDER BY created LIMIT ?",
                (runner_id, limit),
            ):
                job = self.one("SELECT * FROM jobs WHERE id=?", (row["id"],))
                if runner["projects"].get(job["project"]) != job["binding"]:
                    self.db.execute(
                        "UPDATE jobs SET status='failed', result=?,updated=? WHERE id=?",
                        (
                            json.dumps({"error": "Project binding changed; job was not executed"}),
                            time.time(),
                            job["id"],
                        ),
                    )
                    continue
                self.db.execute(
                    "UPDATE jobs SET status='running',instance=?,updated=? WHERE id=? AND status='queued'",
                    (instance, time.time(), job["id"]),
                )
                self.event(job["task_id"], "job.running", {"job_id": job["id"]})
                job["instance"] = instance
                result.append(job)
        return result

    def complete(self, runner: str, job_id: str, instance: str, status: str, result: dict):
        job = self.one("SELECT * FROM jobs WHERE id=? AND runner=?", (job_id, runner))
        if not job or job["instance"] != instance:
            raise ValueError("Job does not belong to this Runner instance")
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid completion status")
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return  # Acknowledgement retry; never rewrite an accepted result.
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,result=?,updated=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), time.time(), job_id),
            )
            self.event(job["task_id"], "job." + status, {"job_id": job_id})

    def approve(self, job_id: str, allow: bool) -> dict:
        with self.db:
            cursor = self.db.execute(
                "UPDATE jobs SET status=?,approved=?,updated=? WHERE id=? AND status='pending_approval'",
                ("queued" if allow else "cancelled", int(allow), time.time(), job_id),
            )
            if not cursor.rowcount:
                raise ValueError("No pending approval for this job")
            row = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            self.event(row["task_id"], "approval.decided", {"job_id": job_id, "allow": allow})
        return self.job(job_id, row["owner"])

    def cancel(self, job_id: str, owner: str) -> dict:
        self.job(job_id, owner)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('queued','pending_approval')",
                (time.time(), job_id),
            )
        return self.job(job_id, owner)

    def close(self):
        self.db.close()
