from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, ValidationError, model_validator

from . import __version__
from .config import StrictModel, identifier, private_dir

PROCESS_STATUSES = {"running", "completed", "failed", "timed_out", "interrupted", "cancelled"}
ResultIdentifier = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(identifier)]


class SubagentObservation(StrictModel):
    model_config = ConfigDict(extra="allow", strict=True)
    agent_id: ResultIdentifier
    status: Literal["running", "completed", "failed", "timed_out", "interrupted", "cancelled"]
    exit_code: Annotated[int, Field(ge=-(2**63), le=2**63 - 1)] | None = None


class RunnerResult(StrictModel):
    model_config = ConfigDict(extra="allow", strict=True)

    registered: bool = False
    project: Annotated[str, Field(min_length=1, max_length=129)] = ""
    process_id: ResultIdentifier = ""
    agent_id: ResultIdentifier = ""
    agents: Annotated[list[SubagentObservation], Field(max_length=100)] = Field(
        default_factory=list
    )
    status: str = ""
    exit_code: Annotated[int, Field(ge=-(2**63), le=2**63 - 1)] | None = None
    has_more: bool = False
    next_cursor: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0

    @model_validator(mode="after")
    def valid_projection(self):
        if self.registered:
            parts = self.project.split("/")
            if len(parts) != 2:
                raise ValueError("A registered project requires an exact runner/project ID")
            for part in parts:
                identifier(part)
        if (
            self.process_id
            and "status" in self.model_fields_set
            and self.status not in PROCESS_STATUSES
        ):
            raise ValueError("A process result requires a valid process status")
        if self.agent_id and self.status not in PROCESS_STATUSES:
            raise ValueError("A subagent result requires a valid status")
        return self


class RunnerCompletion(StrictModel):
    job_id: ResultIdentifier
    instance: ResultIdentifier
    status: Literal["succeeded", "failed", "unknown"]
    result: RunnerResult


def new_id() -> str:
    return uuid.uuid4().hex


def reject_json_constant(value: str):
    raise ValueError("Non-finite JSON result")


class Store:
    def __init__(self, directory: Path):
        self.db = sqlite3.connect(private_dir(directory) / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
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
            CREATE INDEX IF NOT EXISTS task_jobs ON jobs(task_id, updated);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_events ON events(task_id, id);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                question TEXT NOT NULL, answer TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_observations (
                task_id TEXT NOT NULL REFERENCES tasks(id), agent_id TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(id), status TEXT NOT NULL,
                exit_code INTEGER, created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(task_id,agent_id)
            );
        """)
        # Keep added columns and their backfill atomic across interrupted startup.
        self.db.execute("BEGIN IMMEDIATE")
        process_columns_added = False
        for table, column, declaration in (
            ("runners", "project_roots", "TEXT NOT NULL DEFAULT '{}'"),
            ("tasks", "kind", "TEXT NOT NULL DEFAULT 'coding'"),
            ("tasks", "request_id", "TEXT"),
            ("jobs", "process_id", "TEXT"),
            ("jobs", "process_status", "TEXT"),
            ("jobs", "process_exit_code", "INTEGER"),
        ):
            if column not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                process_columns_added |= table == "jobs"
        if process_columns_added:
            # Parse old payloads once; progress queries then read only their small projection.
            for row in self.db.execute(
                "SELECT id,result FROM jobs WHERE status='succeeded' AND result IS NOT NULL "
                "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files')"
            ):
                try:
                    result = RunnerResult.model_validate(
                        json.loads(row["result"], parse_constant=reject_json_constant), strict=True
                    )
                except (ValueError, TypeError):
                    continue
                if result.process_id and result.status:
                    self.db.execute(
                        "UPDATE jobs SET process_id=?,process_status=?,process_exit_code=? WHERE id=?",
                        (result.process_id, result.status, result.exit_code, row["id"]),
                    )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS process_observations ON jobs "
            "(task_id,process_id,updated DESC,id DESC,created,process_status,process_exit_code) "
            "WHERE status='succeeded' AND process_id IS NOT NULL "
            "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files')"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS task_requests ON tasks(owner,request_id) "
            "WHERE request_id IS NOT NULL"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES ('service_id',?)", (new_id(),)
        )
        self.db.commit()
        self.service = {
            "id": self.one("SELECT value FROM metadata WHERE key='service_id'")["value"],
            "product": "webcodex-harness",
            "version": __version__,
        }

    def one(self, sql, params=()) -> dict | None:
        row = self.db.execute(sql, params).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ["projects", "project_roots", "plan", "arguments", "result", "data"]:
            if key in data and data[key] is not None:
                try:
                    data[key] = (
                        json.loads(data[key], parse_constant=reject_json_constant)
                        if key == "result"
                        else json.loads(data[key])
                    )
                except ValueError:
                    if key != "result":
                        raise
                    # job() reports legacy corruption without changing accepted execution state.
        return data

    def rows(self, sql, params=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, params)]

    def event(self, task: str, kind: str, data: dict):
        self.db.execute(
            "INSERT INTO events(task_id,kind,data,created) VALUES (?,?,?,?)",
            (task, kind, json.dumps(data, ensure_ascii=False), time.time()),
        )

    def register(
        self,
        runner: str,
        instance: str,
        projects: dict,
        known_jobs: list[str] | None = None,
        project_roots: dict | None = None,
        refresh_inventory: bool = False,
    ):
        now = time.time()
        previous = self.one("SELECT * FROM runners WHERE id=?", (runner,))
        if refresh_inventory and (not previous or previous["instance"] != instance):
            raise ValueError("Runner must register this instance before refreshing its inventory")
        if previous and previous["instance"] != instance and now - previous["seen"] < 15:
            raise ValueError(
                "Another Runner instance is still connected; retry after its lease expires"
            )
        with self.db:
            if previous and not refresh_inventory:
                for job in self.rows(
                    "SELECT id, task_id FROM jobs WHERE runner=? AND status='running'", (runner,)
                ):
                    if previous["instance"] == instance and job["id"] in (known_jobs or []):
                        continue
                    self.db.execute(
                        "UPDATE jobs SET status='unknown',process_id=NULL,process_status=NULL,"
                        "process_exit_code=NULL,updated=? WHERE id=?",
                        (now, job["id"]),
                    )
                    self.event(job["task_id"], "job.unknown", {"job_id": job["id"]})
            self.db.execute(
                "INSERT INTO runners(id,instance,projects,seen,project_roots) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET instance=excluded.instance,projects=excluded.projects,"
                "seen=excluded.seen,project_roots=excluded.project_roots",
                (runner, instance, json.dumps(projects), now, json.dumps(project_roots or {})),
            )

    def inventory(self, *, roots: bool = False) -> list[dict]:
        result = []
        for row in self.rows("SELECT * FROM runners ORDER BY id"):
            entries = json.loads(row["project_roots"] if roots else row["projects"])
            for project, value in entries.items():
                result.append(
                    {
                        "id": row["id"] + "/" + project,
                        "online": time.time() - row["seen"] < 15,
                        "binding": value["binding"] if roots else value,
                        **({"projects": value["projects"]} if roots else {}),
                    }
                )
        return result

    def create_task(
        self,
        owner: str,
        project: str,
        title: str,
        kind: str = "coding",
        request_id: str | None = None,
    ) -> dict:
        runner, name = project.split("/", 1)
        row = self.one("SELECT * FROM runners WHERE id=?", (runner,))
        entries = (row["project_roots"] if kind == "project" else row["projects"]) if row else {}
        if name not in entries:
            raise ValueError("Project is not registered; start its Runner first")
        binding = entries[name]["binding"] if kind == "project" else entries[name]
        if request_id is not None:
            previous = self.one(
                "SELECT * FROM tasks WHERE owner=? AND request_id=?", (owner, request_id)
            )
            if previous:
                if (
                    previous["runner"],
                    previous["project"],
                    previous["title"],
                    previous["kind"],
                    previous["binding"],
                ) != (runner, name, title, kind, binding):
                    raise ValueError(
                        "request_id already belongs to a different task or project revision"
                    )
                return previous
        if kind == "project":
            previous = self.one(
                "SELECT id FROM tasks WHERE owner=? AND runner=? AND project=? AND binding=? "
                "AND kind='project' ORDER BY created LIMIT 1",
                (owner, runner, name, binding),
            )
            if previous:
                return self.task(previous["id"], owner)
        task, now = new_id(), time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,owner,runner,project,binding,title,created,updated,kind,request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task, owner, runner, name, binding, title, now, now, kind, request_id),
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
        *,
        allow_completed: bool = False,
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
        if task["status"] == "completed" and not allow_completed:
            raise ValueError("Task is completed; use task_update to reopen it first")
        runner = self.one("SELECT * FROM runners WHERE id=?", (task["runner"],))
        binding = (
            runner["project_roots"].get(task["project"], {}).get("binding")
            if runner and task["kind"] == "project"
            else runner["projects"].get(task["project"])
            if runner
            else None
        )
        if binding != task["binding"]:
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
        for key in ["process_id", "process_status", "process_exit_code"]:
            row.pop(key)
        row["job_id"] = row.pop("id")
        row["service"] = self.service
        row["outcome"] = row["status"]
        if row["status"] == "pending_approval":
            target = f"{row['job_id']} --service-id {self.service['id']}"
            row["approval"] = {
                "approve_command": "webcodex approve " + target,
                "deny_command": "webcodex deny " + target,
            }
            row["next_step"] = (
                "This job is waiting for operator approval, not a connection repair. "
                "Use the returned approval command for this WebCodex Harness service; "
                "the job ID is the approval ID. Do not use another project's CLI syntax. "
                "After approval, poll this job_get; do not submit the command again."
            )
        elif row["status"] in {"queued", "running"}:
            runner = self.one("SELECT seen FROM runners WHERE id=?", (row["runner"],))
            row["runner_online"] = bool(runner and time.time() - runner["seen"] < 15)
            row["next_step"] = (
                "The Runner has no recent heartbeat. Report that this job is waiting for the Runner; "
                "do not resubmit it or request approval."
                if not row["runner_online"]
                else "Continue observing this job in the current turn; do not ask the user to say continue."
            )
            row["next_action"] = {
                "tool": "job_get",
                "arguments": {"job_id": job_id, "wait_seconds": 20},
            }
        elif row["status"] == "unknown":
            row["next_step"] = (
                "Execution outcome is uncertain. Inspect repo/process state before starting a replacement."
            )
        try:
            absent = row["result"] is None and row["status"] not in {"succeeded", "failed"}
            RunnerResult.model_validate({} if absent else row["result"], strict=True)
        except ValidationError:
            row["result"] = {
                "error": "The saved result is unavailable; inspect the original execution before retrying.",
                "error_type": "InvalidStoredResult",
            }
            row["next_step"] = "The saved result is unavailable. This job will not be replayed."
            return row
        result = row["result"] or {}
        if row["status"] == "succeeded" and result.get("registered") is True:
            row["next_action"] = {
                "tool": "work_on_project",
                "arguments": {
                    "project": result["project"],
                    "title": "開發：" + result["project"],
                    "request_id": "start:" + job_id,
                },
            }
            row["next_step"] = (
                "The project is registered. Follow next_action to start coding; the project-management "
                "task_id above is not a coding task. No additional approval is needed unless the next "
                "tool actually returns pending_approval."
            )
        if row["status"] == "succeeded" and result.get("process_id") and "status" in result:
            row["outcome"] = result["status"]
            row["next_step"] = (
                "This is the saved process observation. Job succeeded means the tool returned, "
                "not that the command or tests passed. Check outcome, exit_code and output. "
                "approved=0 does not mean approval is pending."
            )
            if result["status"] == "running" or result.get("has_more"):
                row["next_action"] = {
                    "tool": "read_process",
                    "arguments": {
                        "task_id": row["task_id"],
                        "process_id": result["process_id"],
                        "cursor": result.get("next_cursor", 0),
                        "wait_seconds": 1 if result.get("has_more") else 20,
                    },
                }
                row["next_step"] += (
                    " Follow next_action in this turn; never restart to obtain output."
                )
        if row["status"] == "succeeded" and result.get("agent_id"):
            row["outcome"] = result["status"]
            row["next_step"] = (
                "This is a saved subagent observation. Collect final and inspect error/status; "
                "a successful spawn job does not mean the worker finished."
            )
            if result["status"] == "running":
                row["next_action"] = {
                    "tool": "subagent_get",
                    "arguments": {
                        "task_id": row["task_id"],
                        "agent_id": result["agent_id"],
                        "wait_seconds": 20,
                    },
                }
            else:
                row.pop("next_action", None)
        return row

    def task_activity(self, task: dict) -> dict:
        """Describe durable observations without treating the saved handoff as live state."""
        task_id = task["id"]
        counts = {
            row["status"]: row["count"]
            for row in self.rows(
                "SELECT status,count(*) AS count FROM jobs WHERE task_id=? GROUP BY status",
                (task_id,),
            )
        }
        last = self.one("SELECT max(updated) AS updated FROM jobs WHERE task_id=?", (task_id,))
        pending = [
            self.job(row["id"], task["owner"])
            for row in self.rows(
                "SELECT id FROM jobs WHERE task_id=? AND status IN ('pending_approval','queued','running') "
                "ORDER BY status='pending_approval' DESC,created LIMIT 100",
                (task_id,),
            )
        ]
        processes = self.rows(
            "WITH observations AS (SELECT id AS job_id,tool,updated AS observed_at,"
            "process_id,process_status AS status,process_exit_code AS exit_code,"
            "min(created) OVER (PARTITION BY process_id) AS started_at,"
            "row_number() OVER (PARTITION BY process_id ORDER BY updated DESC,id DESC) AS rank "
            "FROM jobs WHERE task_id=? AND status='succeeded' "
            "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files') "
            "AND NOT EXISTS (SELECT 1 FROM agent_observations a "
            "WHERE a.task_id=jobs.task_id AND a.agent_id=jobs.process_id) "
            "AND process_id IS NOT NULL) "
            "SELECT job_id,tool,observed_at,started_at,process_id,status,exit_code FROM observations "
            "WHERE rank=1 ORDER BY started_at DESC,process_id LIMIT 100",
            (task_id,),
        )
        processes.extend(
            self.rows(
                "SELECT job_id,'subagent_get' AS tool,updated AS observed_at,created AS started_at,"
                "agent_id AS process_id,status,exit_code FROM agent_observations "
                "WHERE task_id=? ORDER BY created DESC,agent_id LIMIT 100",
                (task_id,),
            )
        )
        processes.sort(key=lambda process: (-process["started_at"], process["process_id"]))
        processes = processes[:100]
        return {
            "summary_stale": bool(last["updated"] and last["updated"] > task["updated"]),
            "last_job_update": last["updated"],
            "job_counts": counts,
            "pending_jobs": pending,
            "pending_jobs_truncated": sum(
                counts.get(s, 0) for s in ("pending_approval", "queued", "running")
            )
            > len(pending),
            "process_observations": processes,
            "latest_execution": self.job(processes[0]["job_id"], task["owner"])
            if processes
            else None,
        }

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
                job["kind"] = self.one("SELECT kind FROM tasks WHERE id=?", (job["task_id"],))[
                    "kind"
                ]
                binding = (
                    runner["project_roots"].get(job["project"], {}).get("binding")
                    if job["kind"] == "project"
                    else runner["projects"].get(job["project"])
                )
                if binding != job["binding"]:
                    self.db.execute(
                        "UPDATE jobs SET status='failed',result=?,process_id=NULL,process_status=NULL,"
                        "process_exit_code=NULL,updated=? WHERE id=?",
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
        try:
            completion = RunnerCompletion.model_validate(
                {"job_id": job_id, "instance": instance, "status": status, "result": result},
                strict=True,
            )
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("Invalid Runner completion result or envelope") from None
        job = self.one("SELECT * FROM jobs WHERE id=? AND runner=?", (job_id, runner))
        if not job or job["instance"] != instance:
            raise ValueError("Job does not belong to this Runner instance")
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return  # Acknowledgement retry; never rewrite an accepted result.
        projection = completion.result
        observation = bool(projection.process_id and projection.status)
        observed = time.time()
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,result=?,process_id=?,process_status=?,process_exit_code=?,"
                "updated=? WHERE id=?",
                (
                    status,
                    encoded,
                    projection.process_id if observation else None,
                    projection.status if observation else None,
                    projection.exit_code if observation else None,
                    observed,
                    job_id,
                ),
            )
            if status == "succeeded" and job["tool"].startswith("subagent_"):
                agents = projection.agents
                if projection.agent_id:
                    agents = [SubagentObservation.model_validate(result, strict=True)]
                for agent in agents:
                    self.db.execute(
                        "INSERT INTO agent_observations VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(task_id,agent_id) DO UPDATE SET job_id=excluded.job_id,"
                        "status=excluded.status,exit_code=excluded.exit_code,updated=excluded.updated "
                        "WHERE agent_observations.status='running' OR excluded.status!='running'",
                        (
                            job["task_id"],
                            agent.agent_id,
                            job_id,
                            agent.status,
                            agent.exit_code,
                            job["created"],
                            observed,
                        ),
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
