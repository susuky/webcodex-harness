from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .catalog import definitions, tool
from .config import MAX_PROJECTS, ServerConfig, identifier
from .continuation import APP_MIME, APP_URI, Continuations, fingerprint
from .diagnostics import Diagnostics, hash_id
from .projects import PROJECT_OPERATIONS
from .runtime import Runtime
from .store import RunnerCompletion, Store, new_id


class TaskTools:
    def __init__(self, service: Service, owner: str, window: str | None = None):
        self.service, self.store, self.owner = service, service.store, owner
        self.window = window

    def task(self, task_id):
        task = self.store.task(task_id, self.owner)
        self.service.check_project(self.owner, task["runner"] + "/" + task["project"], task["kind"])
        return task

    @tool()
    async def projects_list(self) -> dict:
        """List authorized projects and Runner heartbeat status. Permitted tools execute directly. Schema discovery alone does not verify execution. A host error such as 'This conversation does not support developer MCPs' leaves Runner status unknown. An empty list means no registered projects are visible; use project_roots_list to start a new project. Start a task with an exact returned project ID."""
        allowed = self.service.allowed_projects(self.owner)
        projects = []
        for project in self.store.inventory():
            if project["id"] in allowed:
                project.update(
                    approval_mode="automatic",
                    approval_required=[],
                )
                projects.append(project)
        return {"projects": projects, "service": self.store.service}

    @tool()
    async def project_roots_list(self) -> dict:
        """List allowed parent roots for creating or registering projects, including Runner heartbeat status. Use an exact returned root ID with project_create or project_register; neither needs an existing task. An empty list means the operator has not granted any advertised root to this client. Root access includes projects registered beneath that root, subject to this client's tool permissions."""
        client = self.service.config.clients[self.owner]
        roots = []
        for root in self.store.inventory(roots=True):
            if root["id"] not in client.project_roots:
                continue
            root.update(
                can_create="projects" in client.permissions,
                approval_mode="automatic",
                new_project_approval_mode="automatic",
            )
            roots.append(root)
        return {"roots": roots}

    @tool("write")
    async def task_create(
        self,
        project: str,
        title: Annotated[str, Field(min_length=1, max_length=500)],
        request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None,
    ) -> dict:
        """Create a durable coding task for an explicitly selected project. Save its task ID; subsequent conversations use task_list/task_get to resume the same work. Read workspace_info and applicable instructions before editing."""
        self.service.check_project(self.owner, project)
        return self.store.create_task(self.owner, project, title, request_id=request_id)

    @tool("write")
    async def work_on_project(
        self,
        project: str | None = None,
        task_id: str | None = None,
        title: Annotated[str, Field(min_length=1, max_length=500)] | None = None,
        request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None,
    ) -> dict:
        """Start or resume coding in one call. For new work pass an exact project ID and title; reuse request_id on retries. To continue pass an existing task_id, which supplies its authorized project. Returns current progress plus workspace_info (Git, instructions, tools). Follow next_action to present the task card for this user-initiated turn, then follow workspace.next_action if it is still running. A completed task stays completed until task_update explicitly reopens it. Never infer an ambiguous project or silently create a replacement task."""
        if "read" not in self.service.config.clients[self.owner].permissions:
            raise ValueError("Starting a workspace requires read permission")
        if task_id:
            task = self.task(task_id)
            if task["kind"] != "coding":
                raise ValueError("Select a coding task, not the project creation record")
            if project and project != task["runner"] + "/" + task["project"]:
                raise ValueError("The task belongs to a different project")
        else:
            if not project:
                return {
                    "selection_required": True,
                    **(await self.projects_list()),
                    **(await self.project_roots_list()),
                }
            task = await self.task_create(project, title or "開發：" + project, request_id)
        workspace = await self.service.call(
            self.owner, "workspace_info", {"task_id": task["id"]}, window=self.window
        )
        return {
            "task": await self.task_get(task["id"]),
            "workspace": workspace,
            "next_action": {"tool": "task_present", "arguments": {"task_id": task["id"]}},
        }

    @tool()
    async def task_list(
        self,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict:
        """Find this client's durable tasks across MCP connections and Server restarts. Saved status/summary are handoff notes, not live blockers; follow task_get to read current progress. Transport session IDs are not task IDs."""
        allowed = sorted(self.service.allowed_projects(self.owner))
        roots = self.service.config.clients[self.owner].project_roots
        project_slots = ",".join("?" for _ in allowed) or "NULL"
        root_slots = ",".join("?" for _ in roots) or "NULL"
        rows = self.store.rows(
            "SELECT id,title,runner,project,kind,status,summary,updated FROM tasks WHERE owner=? "
            f"AND ((kind='coding' AND runner||'/'||project IN ({project_slots})) "
            f"OR (kind='project' AND runner||'/'||project IN ({root_slots}))) "
            "ORDER BY updated DESC,rowid DESC LIMIT ? OFFSET ?",
            (self.owner, *allowed, *roots, limit + 1, offset),
        )
        return {
            "tasks": [
                {
                    **r,
                    "next_action": {"tool": "task_get", "arguments": {"task_id": r["id"]}},
                }
                for r in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @tool()
    async def task_get(self, task_id: str) -> dict:
        """Resume a task using progress: pending jobs and latest process observations/output. Saved status/summary/plan may be stale. Follow progress.next_actions with their conditions: task_wait hands off waiting, task_present repairs a missing card, and read_process drains output. While independent work remains, continue it and use observation_actions to refresh the same work. Confirm task_wait succeeds before yielding. This restores saved work records."""
        task = self.task(task_id)
        task["jobs"] = self.store.rows(
            "SELECT id AS job_id,tool,status,created,updated FROM jobs WHERE task_id=? ORDER BY created DESC LIMIT 30",
            (task_id,),
        )
        task["questions"] = self.store.rows(
            "SELECT id,question,answer FROM questions WHERE task_id=? ORDER BY created DESC LIMIT 20",
            (task_id,),
        )
        task["progress"] = self.service.task_progress(task, self.window)
        task["service"] = self.store.service
        task["continuation"] = self.service.task_continuation(task, self.window, task["progress"])
        self.service.route_waiting_progress(task["progress"], task["continuation"])
        return task

    @tool()
    async def task_present(self, task_id: str) -> dict:
        """Show the task progress App for the current turn. Call once in each user-initiated turn that starts or resumes sustained work, even if an earlier turn already showed this task's card. Reuse that card for ordinary tool calls within the turn and for automatic task_resume continuation. The App refreshes authoritative state and can request a later model turn for an explicit task_wait; displaying it does not approve or run work. Clients without App support can keep using task_get/job_get."""
        task = self.task(task_id)
        return self.service.task_presentation(task, self.window)

    @tool()
    async def task_review(
        self,
        task_id: str,
        path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None,
        job_id: str | None = None,
    ) -> dict:
        """Show current changed files (including new files), lazy per-file diffs, and recent real command outcomes/output. Works for active or completed tasks. Omit job_id to refresh; if review_job is pending, repeat this call with its exact job_id instead of creating another review. Set path to an exact listed file for its preview. Changes cover the current project; execution results record recent command outcomes."""
        task = self.task(task_id)
        if task["kind"] != "coding":
            raise ValueError("Review the coding task created for the registered project")
        if job_id:
            job = self.store.job(job_id, self.owner)
            if job["task_id"] != task_id or job["tool"] != "review_changes":
                raise ValueError("This review job belongs to a different task or tool")
            original = self.store.one("SELECT arguments FROM jobs WHERE id=?", (job_id,))[
                "arguments"
            ]
            if original.get("path") != path:
                raise ValueError("Keep the same path while waiting for this review")
            job = await self.job_get(job_id, wait_seconds=1)
        else:
            executions = self.store.rows(
                "SELECT id FROM jobs WHERE task_id=? AND tool IN ('exec_command','skill_run') "
                "AND status IN ('running','succeeded','unknown') ORDER BY created DESC LIMIT 10",
                (task_id,),
            )
            job = await self.service.call(
                self.owner,
                "review_changes",
                {
                    "task_id": task_id,
                    "path": path,
                    "process_ids": [row["id"] for row in executions],
                },
            )
        return {
            "task_id": task_id,
            "summary": task["summary"],
            "review_job": job,
            "review": job.get("result") if job["status"] == "succeeded" else None,
        }

    @tool("write")
    async def task_wait(
        self,
        task_id: str,
        job_ids: Annotated[list[str], Field(min_length=1, max_length=8)],
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
        replace_wait_id: str | None = None,
    ) -> dict:
        """Hand off waiting for existing jobs to the task App, after calling task_present. This persists the exact jobs and never repeats their execution. Use only when no independent work remains in this turn. Tell the user what is running, call this once with a stable request_id, then yield. The App may request a later turn; only task_resume consumes that request. Without a connected App, continue job_get/read_process in the current turn. For manual recovery after the user asks to continue, inspect task_get and actual jobs first; replace_wait_id may name the exact prepared/accepted/unknown wait to invalidate its old notice and start a new wait. Never automatically replace uncertain delivery."""
        task = self.task(task_id)
        if task["status"] == "completed" or task["kind"] != "coding":
            raise ValueError("Only an unfinished coding task can wait for continuation")
        if not self.service.continuations.connected(task_id, self.window):
            raise ValueError(
                "No active task App is connected in this conversation. Continue job_get/read_process in this turn."
            )
        for job_id in job_ids:
            if self.store.job(job_id, self.owner)["task_id"] != task_id:
                raise ValueError("Every waiting job must belong to this exact task")
        self.service.continuations.arm(task_id, job_ids, request_id, replace_wait_id)
        await self.service.refresh_task_wait(task)
        return self.service.task_presentation(task, self.window)

    @tool("write")
    async def task_resume(
        self,
        task_id: str,
        wait_id: str,
        consume_token: Annotated[str, Field(min_length=32, max_length=128)],
    ) -> dict:
        """Consume the exact App continuation before resuming work. Pass the server-provided values unchanged. A duplicate returns already_consumed=true: do not start another workflow or repeat effects. This does not widen existing authorization. After a first successful consume, inspect current progress, report it, continue the authorized task, and present no additional card."""
        task = self.task(task_id)
        if task["status"] == "completed":
            raise ValueError("This task is already completed")
        consumed = self.service.continuations.consume(task_id, wait_id, consume_token)
        return {**consumed, "task": await self.task_get(task_id)}

    @tool()
    async def task_app(
        self,
        task_id: str,
        action: Literal["bind", "state", "prepare", "finish", "pause", "enable", "unbind"],
        view_id: Annotated[str, Field(pattern="^[0-9a-f]{32}$")],
        generation: Annotated[int, Field(ge=1)] | None = None,
        wait_id: str | None = None,
        outcome: Literal["accepted", "unknown"] | None = None,
        delivery_error: Literal[
            "timeout", "rpc_error", "host_rejected", "interrupted", "invalid_prepare"
        ]
        | None = None,
        delivery_error_code: Annotated[int, Field(ge=-(2**31), lt=2**31)] | None = None,
    ) -> dict:
        """App-only coordination for the exact task view. It observes existing work, renews the controller, and fences continuation delivery. It cannot execute a command, approve a job, or consume a model wake."""
        task = self.task(task_id)
        if action != "finish" and (delivery_error is not None or delivery_error_code is not None):
            raise ValueError("Delivery diagnostics belong to the finish action")
        if "write" not in self.service.config.clients[self.owner].permissions:
            if action not in {"bind", "state"}:
                raise ValueError("Continuation control is not permitted for a read-only client")
            return {
                **self.service.task_presentation(task, self.window),
                "control": {"bound": False, "reason": "read_only"},
            }
        continuation = self.service.continuations
        if task["status"] == "completed":
            continuation.cancel(task_id)
        if action == "bind":
            control = continuation.bind(task_id, view_id, self.window)
        else:
            if generation is None:
                raise ValueError("The current view generation is required")
            control = continuation.action(
                task_id, view_id, generation, self.window, action, wait_id, outcome
            )
        if action in {"bind", "state"}:
            await self.service.refresh_task_wait(task)
        return {**self.service.task_presentation(task, self.window), "control": control}

    @tool("write")
    async def task_update(
        self,
        task_id: str,
        status: Literal["active", "waiting", "completed"],
        summary: Annotated[str, Field(max_length=32000)],
        plan: Annotated[list[dict[str, str]], Field(max_length=50)],
    ) -> dict:
        """Save the task's current status, concise handoff summary and plan. Each plan step has step and status (pending/in_progress/completed). Mark completed only after checking the requested result and relevant tests."""
        if self.task(task_id)["kind"] != "coding":
            raise ValueError("Project management records stay open; update the coding task instead")
        for step in plan:
            if (
                set(step) != {"step", "status"}
                or step["status"] not in {"pending", "in_progress", "completed"}
                or not step["step"]
                or len(step["step"]) > 2000
            ):
                raise ValueError("Each plan step requires step and a valid status")
        task = self.store.update_task(task_id, self.owner, status, summary, plan)
        if status == "completed":
            self.service.continuations.cancel(task_id)
        return task

    @tool()
    async def task_events(
        self,
        task_id: str,
        after: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> dict:
        """Read this task's durable event history after a numeric event cursor. The audit trail records tool identity and outcome, not raw command arguments."""
        self.task(task_id)
        events = self.store.rows(
            "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
            (task_id, after, limit),
        )
        for event in events:
            event["data"] = json.loads(event["data"])
        return {"events": events, "next_cursor": events[-1]["id"] if events else after}

    @tool()
    async def job_get(
        self, job_id: str, wait_seconds: Annotated[float, Field(ge=0, le=20)] = 0
    ) -> dict:
        """Observe the same durable job after a timeout/disconnect without repeating execution. Read outcome and the conditional next_action: task_wait hands off waiting, task_present repairs the card, and read_process drains output. While independent work remains, continue it and use observation_action to refresh the same execution. Confirm task_wait succeeds before yielding. Job succeeded only means the tool returned; inspect the process outcome."""
        job = self.store.job(job_id, self.owner)
        self.task(job["task_id"])
        deadline = time.monotonic() + wait_seconds
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            job = self.store.job(job_id, self.owner)
        task = self.task(job["task_id"])
        job["continuation"] = self.service.task_continuation(
            task, self.window, self.service.task_progress(task, self.window)
        )
        self.service.route_waiting_job(job)
        return job

    @tool("write")
    async def job_cancel(self, job_id: str) -> dict:
        """Cancel a queued job. Already-running operations cannot be recalled; use cancel_process once an execution result supplies its process_id."""
        job = self.store.job(job_id, self.owner)
        self.task(job["task_id"])
        return self.store.cancel(job_id, self.owner)

    @tool("write")
    async def request_user_input(
        self, task_id: str, question: Annotated[str, Field(min_length=1, max_length=4000)]
    ) -> dict:
        """Record an out-of-band question for the local operator to answer via CLI. For ordinary clarification or a user already chatting with you, ask directly in the conversation instead; this tool does not display a ChatGPT question UI."""
        self.task(task_id)
        question_id = new_id()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO questions(id,task_id,question,created) VALUES (?,?,?,?)",
                (question_id, task_id, question, time.time()),
            )
            self.store.event(task_id, "question.created", {"question_id": question_id})
        return {"question_id": question_id, "status": "awaiting_user"}

    @tool()
    async def tool_search(
        self,
        query: Annotated[str, Field(max_length=1000)],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Find built-in tool schemas by name or purpose. Installed plugin tools are discovered separately with plugins_list and plugin_tools."""
        terms = query.lower().split()
        specs = self.service.specs(self.owner)
        ranked = sorted(
            specs,
            key=lambda s: sum(w in (s.name + " " + s.description).lower() for w in terms),
            reverse=True,
        )
        return {"tools": [s.model_dump(mode="json", by_alias=True) for s in ranked[:limit]]}


REMOTE = definitions(Runtime, remote=True)
LOCAL = definitions(TaskTools, remote=False)


class Service:
    def __init__(self, config: ServerConfig):
        self.app_html = (Path(__file__).parent / "task_app.html").read_text(encoding="utf-8")
        self.app_uri = f"{APP_URI}/{hashlib.sha256(self.app_html.encode()).hexdigest()[:16]}.html"
        self.config, self.store = config, Store(config.state_dir)
        self.diagnostics = Diagnostics("server", config.state_dir / "diagnostics")
        self.observe_tool_activity(self.store.interrupt_tool_activity)
        self.continuations = Continuations(self.store)
        self.resume_legacy_jobs()

    async def refresh_task_wait(self, task: dict):
        wait = self.continuations.current(task["id"])
        if not wait or wait["state"] != "watching":
            return
        if task["status"] == "completed":
            self.continuations.cancel(task["id"])
            return
        jobs, ready = [], True
        for job_id in json.loads(wait["jobs"]):
            job = self.store.job(job_id, task["owner"])
            result = job.get("result") or {}
            if (
                job["status"] == "succeeded"
                and result.get("status") == "running"
                and result.get("process_id")
            ):
                job = await self.call(
                    task["owner"],
                    "read_process",
                    {
                        "task_id": task["id"],
                        "process_id": result["process_id"],
                        "wait_seconds": 0,
                        "request_id": "watch:" + wait["id"] + ":" + job_id,
                    },
                )
            jobs.append(job["job_id"])
            if job["status"] in {"queued", "running", "pending_approval"} or (
                job["status"] == "succeeded"
                and (job.get("result") or {}).get("status") == "running"
            ):
                ready = False
        self.continuations.refresh(wait["id"], wait["jobs"], jobs, ready)

    def task_continuation(self, task: dict, window: str | None, progress: dict) -> dict:
        continuation = self.continuations.public(task["id"])
        wait = continuation["wait"]
        handoff = {"state": "not_armed", "condition": "only_when_no_independent_work"}
        reason = None
        if task["status"] == "completed":
            reason = "completed"
        elif task["kind"] != "coding":
            reason = "unsupported_task"
        elif "write" not in self.config.clients[task["owner"]].permissions:
            reason = "read_only"
        elif continuation["paused"]:
            reason = "paused"
        elif progress["state"] in {
            "project_changed",
            "waiting_operator",
            "waiting_runner",
        }:
            reason = progress["state"]
        elif window is None:
            reason = "missing_window"
        else:
            controller = self.store.one(
                "SELECT window_hash,lease_until FROM task_controllers WHERE task_id=?",
                (task["id"],),
            )
            if not controller:
                reason = "not_connected"
            elif controller["window_hash"] != window:
                reason = "another_window"
            elif controller["lease_until"] <= time.time():
                reason = "expired"
            elif wait and wait["state"] in {"watching", "ready", "prepared", "accepted", "unknown"}:
                handoff["state"] = wait["state"]
            elif progress["pending_jobs_truncated"]:
                reason = "incomplete_job_list"
            else:
                # Check all process histories: task_activity only lists the newest 100 processes.
                running = self.store.rows(
                    "WITH latest AS (SELECT id,process_status,"
                    "row_number() OVER (PARTITION BY process_id ORDER BY updated DESC,id DESC) AS rank "
                    "FROM jobs WHERE task_id=? AND status='succeeded' AND process_id IS NOT NULL "
                    "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files') "
                    "AND NOT EXISTS (SELECT 1 FROM agent_observations a "
                    "WHERE a.task_id=jobs.task_id AND a.agent_id=jobs.process_id)) "
                    "SELECT id FROM latest WHERE rank=1 AND process_status='running' "
                    "UNION SELECT agent_id AS id FROM agent_observations "
                    "WHERE task_id=? AND status='running' LIMIT 9",
                    (task["id"], task["id"]),
                )
                jobs = sorted(
                    {job["job_id"] for job in progress["pending_jobs"]}
                    | {job["id"] for job in running}
                )
                if len(jobs) > 8:
                    reason = "too_many_jobs"
                elif not jobs:
                    handoff["state"] = wait["state"] if wait else "not_armed"
                    handoff["reason"] = "no_pending_jobs"
                else:
                    handoff["state"] = "available"
                    request = json.dumps([task["id"], jobs, wait["id"] if wait else None])
                    handoff["next_action"] = {
                        "tool": "task_wait",
                        "arguments": {
                            "task_id": task["id"],
                            "job_ids": jobs,
                            "request_id": "handoff:" + fingerprint(request),
                        },
                    }
        if reason:
            handoff.update(state="blocked", reason=reason)
            if reason in {"not_connected", "expired"}:
                handoff["next_action"] = {
                    "tool": "task_present",
                    "arguments": {"task_id": task["id"]},
                }
        return {**continuation, "handoff": handoff}

    @staticmethod
    def route_waiting_job(job: dict):
        continuation = job["continuation"]
        handoff = continuation["handoff"]
        result = job.get("result") or {}
        waiting = job["status"] in {"queued", "running"} or (
            job["status"] == "succeeded" and result.get("status") == "running"
        )
        action = job.get("next_action")
        if waiting and action and not result.get("has_more"):
            if handoff.get("next_action"):
                job["observation_action"] = action
                job["next_action"] = {
                    **handoff["next_action"],
                    "condition": "only_when_no_independent_work",
                }
                job["next_step"] = (
                    "Inspect the current result. If only waiting remains, follow next_action. "
                    "For task_wait, confirm success and yield; for task_present, wait for the card "
                    "to bind and recheck the original job. If independent work remains, continue it "
                    "and use observation_action when you need another observation."
                )
            elif handoff["state"] in {"watching", "ready", "prepared", "accepted", "unknown"}:
                job["observation_action"] = job.pop("next_action")
                job["next_step"] = Service.handoff_guidance(continuation).strip()
            else:
                job["next_step"] = job.get("next_step", "") + Service.handoff_guidance(continuation)
        else:
            job["next_step"] = job.get("next_step", "") + Service.handoff_guidance(continuation)

    @staticmethod
    def route_waiting_progress(progress: dict, continuation: dict):
        handoff = continuation["handoff"]
        actions = progress["next_actions"]
        only_observations = actions and all(
            action["tool"] in {"job_get", "read_process"} for action in actions
        )
        output_actions = progress["output_actions"]
        has_output = bool(output_actions)
        has_issues = bool(progress["recent_issues"] or progress["recent_process_issues"])
        if has_output and progress["state"] not in {
            "waiting_runner",
            "project_changed",
            "completed",
        }:
            progress["next_actions"] = output_actions + [
                action for action in actions if action not in output_actions
            ]
        if has_issues:
            progress["next_step"] += (
                " Inspect recent_issues and recent_process_issues before yielding; "
                "preserve any existing continuation while diagnosing these results."
            )
            return
        if progress["state"] == "observing" and only_observations and not has_output:
            if handoff.get("next_action"):
                progress["observation_actions"] = actions
                progress["next_actions"] = [
                    {**handoff["next_action"], "condition": "only_when_no_independent_work"}
                ]
                progress["next_step"] = (
                    "Inspect current results. If only waiting remains, follow next_actions: "
                    "confirm task_wait succeeds and yield, or present the card and recheck after it binds. "
                    "If independent work remains, continue it; observation_actions refresh the original work."
                )
                return
            if handoff["state"] in {"watching", "ready", "prepared", "accepted", "unknown"}:
                progress["observation_actions"], progress["next_actions"] = actions, []
                progress["next_step"] = Service.handoff_guidance(continuation).strip()
                return
        progress["next_step"] += Service.handoff_guidance(continuation)

    @staticmethod
    def handoff_guidance(continuation: dict) -> str:
        handoff = continuation["handoff"]
        if handoff["state"] == "available":
            return (
                " If no independent work remains, call continuation.handoff.next_action with its exact "
                "arguments, then yield. Otherwise continue working in this turn. Automatic continuation "
                "requires a successful task_wait; an available handoff has not been armed."
            )
        if handoff.get("reason") in {
            "missing_window",
            "not_connected",
            "another_window",
            "expired",
        }:
            return (
                " To hand off waiting, call task_present for this task in this conversation, then recheck "
                "continuation.handoff after the card binds. Presenting alone does not confirm a connected "
                "card. Until handoff is available, keep observing the original jobs in this turn."
            )
        if handoff["state"] in {"watching", "ready"}:
            return (
                " Waiting has been handed off to the task App. After a successful task_wait, report "
                "what is running and yield; the App observes those jobs. On its continuation message, "
                "call task_resume before resuming work."
            )
        if handoff["state"] in {"prepared", "accepted", "unknown"}:
            return " Preserve the current continuation; do not arm or replace an uncertain delivery automatically."
        return ""

    def task_presentation(self, task: dict, window: str | None = None) -> dict:
        progress = self.task_progress(task, window)
        continuation = self.task_continuation(task, window, progress)
        return {
            "task": {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "plan": task["plan"],
            },
            "progress": {
                "state": progress["state"],
                "runner_online": progress["runner_online"],
                "pending_count": len(progress["pending_jobs"]),
                "updated": progress["last_job_update"] or task["updated"],
                "next_step": progress["next_step"] + self.handoff_guidance(continuation),
                "liveness": progress["liveness"],
            },
            "continuation": continuation,
        }

    def resume_legacy_jobs(self):
        # Upgrade old queues in place; revoked access and completed tasks never execute.
        with self.store.db:
            for job in self.store.rows(
                "SELECT jobs.*,tasks.kind,tasks.status AS task_status FROM jobs "
                "JOIN tasks ON tasks.id=jobs.task_id WHERE jobs.status='pending_approval'"
            ):
                client = self.config.clients.get(job["owner"])
                definition = (PROJECT_OPERATIONS if job["kind"] == "project" else REMOTE).get(
                    job["tool"]
                )
                project = job["runner"] + "/" + job["project"]
                allowed = bool(
                    client and definition and definition.permission in client.permissions
                )
                try:
                    self.check_project(job["owner"], project, job["kind"])
                except (KeyError, ValueError):
                    allowed = False
                status = "queued" if allowed and job["task_status"] != "completed" else "cancelled"
                self.store.db.execute(
                    "UPDATE jobs SET status=?,instance=NULL,updated=? "
                    "WHERE id=? AND status='pending_approval'",
                    (status, time.time(), job["id"]),
                )
                self.store.event(
                    job["task_id"],
                    "job.legacy_queue_migrated",
                    {"job_id": job["id"], "status": status},
                )

    def allowed_projects(self, owner: str) -> set[str]:
        client = self.config.clients[owner]
        allowed = set(client.projects)
        for root in self.store.inventory(roots=True):
            if root["id"] in client.project_roots:
                runner = root["id"].split("/", 1)[0]
                allowed.update(runner + "/" + name for name in root["projects"])
        return allowed

    def check_project(self, owner: str, project: str, kind: str = "coding"):
        allowed = (
            self.config.clients[owner].project_roots
            if kind == "project"
            else self.allowed_projects(owner)
        )
        if project not in allowed:
            raise ValueError("Project is not authorized for this client")

    def task_progress(self, task: dict, window: str | None = None) -> dict:
        activity = self.store.task_activity(task)
        work = activity.pop("work")
        runner = self.store.one("SELECT * FROM runners WHERE id=?", (task["runner"],))
        online = bool(runner and time.time() - runner["seen"] < 15)
        binding = (
            runner["project_roots"].get(task["project"], {}).get("binding")
            if runner and task["kind"] == "project"
            else runner["projects"].get(task["project"])
            if runner
            else None
        )
        pending = activity["pending_jobs"]
        actions = [job["next_action"] for job in pending if "next_action" in job]
        running = [p for p in activity["process_observations"] if p["status"] == "running"]
        output_actions = []
        for process in activity["process_observations"]:
            observation = self.store.job(process["job_id"], task["owner"])
            action = observation.get("next_action")
            if action and (observation.get("result") or {}).get("has_more"):
                output_actions.append(action)
            if process["status"] != "running":
                continue
            actions.append(
                action
                or {
                    "tool": "read_process",
                    "arguments": {
                        "task_id": task["id"],
                        "process_id": process["process_id"],
                        "wait_seconds": 20,
                    },
                }
            )
        issues = self.store.rows(
            "SELECT id AS job_id,tool,status FROM jobs WHERE task_id=? AND updated>? "
            "AND status IN ('failed','unknown') ORDER BY updated DESC LIMIT 30",
            (task["id"], task["updated"]),
        )
        for issue in issues:
            actions.append({"tool": "job_get", "arguments": {"job_id": issue["job_id"]}})
        failed = [
            p
            for p in activity["process_observations"]
            if p["status"] in {"failed", "timed_out", "interrupted", "cancelled"}
            and p["observed_at"] > task["updated"]
        ]
        for process in failed:
            legacy_worker = process["tool"].startswith("subagent_")
            actions.append(
                {
                    "tool": "job_get" if legacy_worker else "read_process",
                    "arguments": {"job_id": process["job_id"]}
                    if legacy_worker
                    else {
                        "task_id": task["id"],
                        "process_id": process["process_id"],
                    },
                }
            )
        questions = self.store.one(
            "SELECT count(*) AS count FROM questions WHERE task_id=? AND answer IS NULL",
            (task["id"],),
        )["count"]
        if binding != task["binding"]:
            state, message = (
                "project_changed",
                "The registered project changed. Inspect its identity before creating a replacement task.",
            )
        elif task["status"] == "completed":
            state, message = (
                "completed",
                "The saved task is completed. Reopen it with task_update only if more work is requested.",
            )
        elif (pending or running) and not online:
            state, message = (
                "waiting_runner",
                "The Runner has no recent heartbeat. Preserve these jobs and wait for the Runner.",
            )
        elif questions:
            state, message = (
                "waiting_operator",
                "An out-of-band operator question is unanswered. Ordinary clarification belongs in the current conversation.",
            )
        elif pending or running:
            state, message = (
                "observing",
                "Follow next_actions now. Process entries are saved observations; read_process refreshes their actual state. Do not ask the user to say continue.",
            )
        elif issues or failed:
            state, message = (
                "needs_attention",
                "New execution problems were recorded after the saved handoff. Inspect latest_execution and recent_issues to diagnose the failure.",
            )
        else:
            state, message = (
                "ready",
                "No queued job blocks this task. Continue the authorized work and refresh the handoff; a saved waiting status is not a current blocker.",
            )
        return {
            "state": state,
            "next_step": message,
            "approval_mode": "automatic",
            "approval_required": [],
            "runner_online": online,
            **activity,
            "recent_issues": issues,
            "recent_process_issues": failed,
            "unanswered_operator_questions": questions,
            "next_actions": []
            if state in {"waiting_runner", "project_changed", "completed"}
            else actions,
            "output_actions": output_actions,
            "liveness": {
                "observed_at": time.time(),
                "runner_last_seen": runner["seen"] if runner else None,
                "conversation": self.store.tool_activity(task["id"], window),
                "work": work,
            },
        }

    def specs(self, owner: str) -> list[types.Tool]:
        permissions = self.config.clients[owner].permissions
        specs = [
            d.spec()
            for d in [*LOCAL.values(), *REMOTE.values(), *PROJECT_OPERATIONS.values()]
            if d.permission in permissions
        ]
        for spec in specs:
            if spec.name == "task_present":
                spec.meta = {
                    "ui": {"resourceUri": self.app_uri},
                    "openai/outputTemplate": self.app_uri,
                    "openai/widgetAccessible": True,
                }
            elif spec.name == "task_app":
                spec.meta = {
                    "ui": {"visibility": ["app"]},
                    "openai/widgetAccessible": True,
                    "openai/visibility": "private",
                }
            elif spec.name == "task_review":
                spec.meta = {
                    "ui": {"visibility": ["model", "app"]},
                    "openai/widgetAccessible": True,
                }
            else:
                spec.meta = {"ui": {"visibility": ["model"]}}
        return specs

    def claim(self, runner: str, instance: str, limit: int) -> list[dict]:
        allowed = []
        for job in self.store.claim(runner, instance, limit):
            client = self.config.clients.get(job["owner"])
            definition = (PROJECT_OPERATIONS if job["kind"] == "project" else REMOTE).get(
                job["tool"]
            )
            projects = (
                client.project_roots
                if client and job["kind"] == "project"
                else self.allowed_projects(job["owner"])
                if client
                else set()
            )
            if (
                not client
                or not definition
                or definition.permission not in client.permissions
                or job["runner"] + "/" + job["project"] not in projects
            ):
                self.store.complete(
                    runner,
                    job["id"],
                    instance,
                    "failed",
                    {"error": "Authorization was revoked before dispatch; job was not executed"},
                )
            else:
                allowed.append(job)
        return allowed

    def tool_activity_task(
        self, owner: str, name: str, arguments: dict, result: dict | None = None
    ) -> str | None:
        if not isinstance(name, str) or name in {"task_app", "task_review"}:
            return None
        definition = LOCAL.get(name) or REMOTE.get(name) or PROJECT_OPERATIONS.get(name)
        if definition is None:
            return None
        try:
            if result is not None:
                if name == "task_create":
                    task_id = result.get("id")
                elif name == "work_on_project":
                    task_id = (result.get("task") or {}).get("id")
                else:
                    task_id = result.get("task_id")
            elif not isinstance(arguments, dict):
                return None
            elif name in {"job_get", "job_cancel"}:
                job_id = arguments.get("job_id")
                if not isinstance(job_id, str):
                    return None
                task_id = self.store.job(job_id, owner)["task_id"]
            elif "task_id" in definition.model.model_fields:
                task_id = arguments.get("task_id")
            else:
                return None
            if isinstance(task_id, str):
                return TaskTools(self, owner).task(task_id)["id"]
        except (ValueError, KeyError):
            pass
        return None

    def observe_tool_activity(self, operation, *args, **kwargs):
        try:
            operation(*args, **kwargs)
        except sqlite3.Error as error:
            self.diagnostics.record("activity.error", exception_type=type(error).__name__)

    async def traced_call(
        self,
        owner: str,
        name: str,
        arguments: dict,
        *,
        window: str | None = None,
        diagnostic_context: dict | None = None,
        track_activity: bool = False,
    ) -> dict:
        fields = {
            "trace_id": new_id(),
            **(diagnostic_context or {}),
            "tool": name
            if isinstance(name, str) and name in (LOCAL | REMOTE | PROJECT_OPERATIONS)
            else "unknown",
        }
        if isinstance(arguments, dict):
            for key in ("task_id", "job_id", "wait_id"):
                fields[key] = arguments.get(key)
            if name == "task_app":
                for key in ("action", "delivery_error", "delivery_error_code"):
                    fields[key] = arguments.get(key)
                fields["delivery_outcome"] = arguments.get("outcome")
        started = time.monotonic()
        activity_started = time.time()
        activity_task = self.tool_activity_task(owner, name, arguments) if track_activity else None
        activity_trace = new_id()
        if activity_task:
            self.observe_tool_activity(
                self.store.start_tool_activity,
                activity_task,
                activity_trace,
                name,
                activity_started,
                window=window,
            )
        self.diagnostics.record("tool.started", **fields)
        try:
            result = await self.call(owner, name, arguments, window=window)
        except asyncio.CancelledError:
            if activity_task:
                self.observe_tool_activity(
                    self.store.finish_tool_activity,
                    activity_task,
                    activity_trace,
                    activity_started,
                    "cancelled",
                )
            self.diagnostics.record(
                "tool.cancelled", **fields, duration_ms=(time.monotonic() - started) * 1000
            )
            raise
        except Exception as error:
            if activity_task:
                self.observe_tool_activity(
                    self.store.finish_tool_activity,
                    activity_task,
                    activity_trace,
                    activity_started,
                    "error",
                )
            self.diagnostics.record(
                "tool.error",
                **fields,
                duration_ms=(time.monotonic() - started) * 1000,
                exception_type=type(error).__name__,
            )
            raise
        fields["job_id"] = result.get("job_id", fields.get("job_id"))
        fields["task_id"] = result.get("task_id", fields.get("task_id"))
        if name in {"task_create", "task_get"}:
            fields["task_id"] = result.get("id", fields.get("task_id"))
        wait = (result.get("continuation") or {}).get("wait") or {}
        fields.update(wait_id=wait.get("id", fields.get("wait_id")), wait_state=wait.get("state"))
        handoff = (result.get("continuation") or {}).get("handoff") or {}
        control = result.get("control") or {}
        fields.update(
            handoff_state=handoff.get("state"),
            handoff_reason=handoff.get("reason"),
            control_bound=control.get("bound"),
            control_reason=control.get("reason"),
            controller_generation=control.get("generation"),
        )
        payload = result.get("result")
        payload = payload if isinstance(payload, dict) else {}
        native = payload.get("mcp_result")
        native = native if isinstance(native, dict) else {}
        is_error = (
            result.get("status") in {"failed", "cancelled", "unknown"}
            or native.get("isError") is True
        )
        if track_activity and activity_task is None:
            activity_task = self.tool_activity_task(owner, name, arguments, result)
            if activity_task:
                self.observe_tool_activity(
                    self.store.start_tool_activity,
                    activity_task,
                    activity_trace,
                    name,
                    activity_started,
                    late_bound=True,
                    window=window,
                )
        if activity_task:
            self.observe_tool_activity(
                self.store.finish_tool_activity,
                activity_task,
                activity_trace,
                activity_started,
                "error" if is_error else "returned",
            )
        self.diagnostics.record(
            "tool.completed",
            **fields,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome=result.get("status", "returned"),
            process_outcome=payload.get("outcome", payload.get("status")),
            exit_code=payload.get("exit_code"),
            is_error=is_error,
        )
        return result

    async def call(
        self, owner: str, name: str, arguments: dict, *, window: str | None = None
    ) -> dict:
        definition = LOCAL.get(name) or REMOTE.get(name) or PROJECT_OPERATIONS.get(name)
        if definition is None:
            raise ValueError("Unknown tool; use tool_search or list available tools")
        client = self.config.clients[owner]
        if definition.permission not in client.permissions:
            raise ValueError("Tool is not permitted for this client")
        args = definition.model.model_validate(arguments).model_dump()
        local = TaskTools(self, owner, window)
        if name in LOCAL:
            return await getattr(local, name)(**args)
        if name in PROJECT_OPERATIONS:
            self.check_project(owner, args["root"], "project")
            task = self.store.create_task(
                owner, args["root"], "專案管理：" + args["root"], "project"
            )
            request_id = args["request_id"]
        else:
            task = local.task(args.pop("task_id"))
            if task["kind"] != "coding":
                raise ValueError("Use task_create with the registered project ID before coding")
            request_id = args.pop("request_id")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ValueError("request_id must contain 1–128 characters")
        job = self.store.enqueue(
            task,
            name,
            args,
            request_id,
            self.config.max_pending_jobs,
            allow_completed=definition.permission == "read",
        )
        return await local.job_get(job["job_id"], wait_seconds=0.75)


def mcp_result(result: dict) -> types.CallToolResult:
    native = (result.get("result") or {}).get("mcp_result")
    content = []
    error = result.get("status") in {"failed", "cancelled", "unknown"}
    if native:
        converted = types.CallToolResult.model_validate(native)
        content.extend(converted.content)
        error = error or converted.isError
        result = dict(result)
        result["result"] = {k: v for k, v in result["result"].items() if k != "mcp_result"}
        if converted.structuredContent is not None:
            result["result"]["provider_result"] = converted.structuredContent
    content.insert(0, types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False)))
    return types.CallToolResult(content=content, structuredContent=result, isError=error)


class RequestDiagnostics:
    """Observe ASGI traffic without reading ahead or changing disconnect handling."""

    def __init__(self, app, diagnostics: Diagnostics):
        self.app, self.diagnostics = app, diagnostics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        routes = {
            "/mcp",
            "/healthz",
            "/api/call",
            "/api/tools",
            "/api/runner/register",
            "/api/runner/poll",
            "/api/runner/complete",
            "/api/admin/approvals",
            "/api/admin/questions",
        }
        route = path if path in routes else "unknown"
        for prefix in ("/api/admin/approvals/", "/api/admin/questions/"):
            if path.startswith(prefix):
                route = prefix + ":id"
        http_id = new_id()
        scope.setdefault("state", {})["http_id"] = http_id
        fields = {"http_id": http_id, "route": route, "method": scope["method"]}
        quiet = route in {"/healthz", "/api/runner/poll"}
        started = time.monotonic()
        status, size, disconnected, completed = None, 0, False, False
        event = "http.completed"
        if not quiet:
            self.diagnostics.record("http.started", **fields)

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            disconnected |= message["type"] == "http.disconnect"
            return message

        async def observed_send(message):
            nonlocal status, size, completed
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
                completed |= not message.get("more_body", False)

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            event = "http.cancelled"
            raise
        except Exception as error:
            event = "http.error"
            fields["exception_type"] = type(error).__name__
            raise
        finally:
            if event == "http.completed" and disconnected and not completed:
                event = "http.disconnected"
            if not quiet or event != "http.completed" or status != 200:
                self.diagnostics.record(
                    event,
                    **fields,
                    http_status=status,
                    response_bytes=size,
                    disconnected=disconnected,
                    duration_ms=(time.monotonic() - started) * 1000,
                )


class Security:
    def __init__(self, app, config: ServerConfig):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        header = headers.get(b"authorization", b"").decode("latin-1")
        token = header[7:] if header.startswith("Bearer ") else ""
        owner = None
        if path.startswith("/api/runner/"):
            owner = next(
                (
                    name
                    for name, secret in self.config.runners.items()
                    if hmac.compare_digest(secret.encode(), token.encode())
                ),
                None,
            )
        elif path.startswith("/api/admin/"):
            owner = (
                "admin"
                if hmac.compare_digest(self.config.admin_token.encode(), token.encode())
                else None
            )
        else:
            owner = next(
                (
                    name
                    for name, client in self.config.clients.items()
                    if hmac.compare_digest(client.token.encode(), token.encode())
                ),
                None,
            )
        if owner is None:
            return await JSONResponse(
                {"error": "Unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        if b"origin" in headers:
            # Browser requests need the exact operator-configured origin; token auth remains mandatory.
            if headers[b"origin"].decode("latin-1").rstrip("/") != self.config.public_url.rstrip(
                "/"
            ):
                return await JSONResponse({"error": "Origin is not allowed"}, 403)(
                    scope, receive, send
                )
        scope.setdefault("state", {})["owner"] = owner
        size, messages = 0, []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 4 * 1024 * 1024:
                return await JSONResponse({"error": "Request exceeds 4 MiB"}, 413)(
                    scope, receive, send
                )
            messages.append(message)
            if not message.get("more_body", False):
                break
        buffered = iter(messages)

        async def bounded_receive():
            try:
                return next(buffered)
            except StopIteration:
                return await receive()

        try:
            await self.app(scope, bounded_receive, send)
        except (ValueError, ValidationError, KeyError, TypeError) as error:
            await JSONResponse({"error": str(error)[:1000]}, 400)(scope, receive, send)


def create_app(config: ServerConfig):
    service = Service(config)
    mcp = Server(
        "webcodex",
        version=__version__,
        instructions=(
            "You can work on repositories through this coding harness. For existing projects, use projects_list, "
            "then work_on_project(project, title, request_id) to start, or work_on_project(task_id) to resume. "
            "The returned workspace already contains the initial Git status and root instructions; avoid "
            "repeating those reads without a reason. task_create and task_list/task_get remain available. Read applicable "
            "AGENTS.md and matching skills before changing code. Preserve unrelated edits. Use file hashes for "
            "edits and run relevant tests. Use task_review to show changed files, new untracked files, "
            "per-file diffs and actual execution results before closeout. Reviews remain available after completion. "
            "Save the current summary/plan with task_update. "
            "On resume, task_get.progress takes precedence over saved status/summary/plan. A stale waiting "
            "summary is not a current blocker. Read latest_execution: job status succeeded only means the "
            "tool returned; outcome/exit_code can show command failure. "
            "Follow returned next_action/next_actions and their conditions. While independent work "
            "remains, continue it and use observation_action/observation_actions to refresh existing work. "
            "When only waiting remains and next_action is task_wait, call it with the exact arguments, "
            "confirm success, then yield. An available handoff is not armed until "
            "task_wait succeeds. Otherwise keep observing existing jobs in this turn until work finishes "
            "or a real blocker needs user input; do not require repeated 'continue' messages to poll. "
            "Tell the user what is starting before a long operation. While working, give a brief visible "
            "update when the state changes or after about 30 seconds of waiting; don't silently chain "
            "tool calls. Base updates on observed results, not estimated completion percentages. "
            "In each user-initiated turn that starts or resumes sustained work, call task_present "
            "once to show the task progress App, even if an earlier turn already showed it. "
            "Follow work_on_project.next_action to present it. Reuse that card for ordinary tool "
            "calls within the turn and for automatic task_resume continuation. "
            "When only existing jobs remain to wait for and the App is connected, call task_wait "
            "with those exact job IDs, tell the user what is running, and yield. The App can request "
            "a later turn. On that request call task_resume first; already_consumed=true means "
            "another turn already owns that continuation, so do not repeat work. If no App is "
            "connected, keep following job_get/read_process during this turn. "
            "Ask ordinary clarification directly in the conversation; request_user_input records an "
            "out-of-band CLI question and is not the ChatGPT question UI. "
            "Reasoning and any subagent orchestration stay in ChatGPT or the MCP client. "
            "WebCodex supplies host tools only; do not launch Codex CLI or another model backend "
            "to substitute for the client's native subagents. "
            "Long calls return job_id; poll job_get for the same job. Command results return process_id; use "
            "read_process/write_stdin instead of launching a duplicate. Authorized tools execute directly "
            "without a separate approval queue or project trust setting. "
            "Distinguish tool discovery, host execution permission and Runner availability. If the host "
            "rejects developer MCP execution, stop retrying and report host execution blocked with Runner "
            "status unknown from that attempt. Only a successful projects_list or project_roots_list provides Runner "
            "status; never translate host or transport errors into an offline result."
            " For a new project, use project_roots_list then project_create (or project_register for an "
            "existing directory). Poll its job_get until registration succeeds, then follow "
            "the returned next_action (work_on_project) or task_create with the returned project ID. "
            "Project roots require operator configuration; a new "
            "project does not require an existing coding task."
        ),
    )

    @mcp.list_tools()
    async def list_tools():
        return service.specs(mcp.request_context.request.state.owner)

    @mcp.list_resources()
    async def list_resources():
        owner = mcp.request_context.request.state.owner
        if "read" not in service.config.clients[owner].permissions:
            return []
        return [
            types.Resource(
                uri=service.app_uri, name="task-progress", title="任務進度", mimeType=APP_MIME
            )
        ]

    # The resource has no task data or credentials. All live data goes through authenticated tools.
    @mcp.read_resource()
    async def read_resource(uri):
        owner = mcp.request_context.request.state.owner
        uri = str(uri)
        if "read" not in service.config.clients[owner].permissions or (
            uri != APP_URI and not re.fullmatch(re.escape(APP_URI) + r"/[0-9a-f]{16}\.html", uri)
        ):
            raise ValueError("Resource not found")
        return [
            ReadResourceContents(
                service.app_html,
                APP_MIME,
                {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}},
            )
        ]

    @mcp.call_tool()
    async def call_tool(name, arguments):
        context = mcp.request_context
        token = context.meta.progressToken if context.meta else None
        meta = context.meta.model_dump(by_alias=True, exclude_none=True) if context.meta else {}
        raw_window = meta.get("openai/session")
        window = (
            fingerprint("window\0" + raw_window)
            if (
                isinstance(raw_window, str)
                and 0 < len(raw_window) <= 256
                and all(33 <= ord(c) <= 126 for c in raw_window)
            )
            else None
        )
        bridge_trace = meta.get("webcodex/trace_id")
        diagnostic_context = {
            "trace_id": bridge_trace
            if isinstance(bridge_trace, str) and re.fullmatch(r"[0-9a-f]{32}", bridge_trace)
            else new_id(),
            "bridge_trace_id": bridge_trace,
            "http_id": getattr(context.request.state, "http_id", None),
            "rpc_id_hash": hash_id(context.request_id),
            "window_hash": window,
        }

        async def heartbeat():
            started = time.monotonic()
            while True:
                await asyncio.sleep(5)
                elapsed = time.monotonic() - started
                try:
                    await context.session.send_progress_notification(
                        token,
                        elapsed,
                        message=f"正在確認工作狀態，已等候 {int(elapsed)} 秒。",
                        related_request_id=context.request_id,
                    )
                except Exception as error:
                    service.diagnostics.record(
                        "progress.error",
                        **diagnostic_context,
                        exception_type=type(error).__name__,
                    )
                    return

        # Progress belongs to this request only; a failed notification must not cancel a durable job.
        progress = asyncio.create_task(heartbeat()) if token is not None else None
        try:
            return mcp_result(
                await service.traced_call(
                    context.request.state.owner,
                    name,
                    arguments,
                    window=window,
                    diagnostic_context=diagnostic_context,
                    track_activity=True,
                )
            )
        except (ValueError, ValidationError) as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error)[:2000])], isError=True
            )
        finally:
            if progress is not None:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)

    manager = StreamableHTTPSessionManager(
        mcp,
        json_response=False,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with manager.run():
                yield
        finally:
            try:
                service.store.close()
            finally:
                service.diagnostics.close()

    async def health(request):
        return JSONResponse(
            {"status": "ok", "version": __version__, "service": service.store.service}
        )

    async def api_tools(request):
        return JSONResponse(
            {
                "tools": [
                    s.model_dump(mode="json", by_alias=True)
                    for s in service.specs(request.state.owner)
                ]
            }
        )

    async def api_call(request):
        body = await request.json()
        return JSONResponse(
            await service.traced_call(
                request.state.owner,
                body["name"],
                body.get("arguments", {}),
                diagnostic_context={"http_id": getattr(request.state, "http_id", None)},
            )
        )

    async def register(request: Request):
        body = await request.json()
        identifier(body["instance"])
        projects = body["projects"]
        if not isinstance(projects, dict) or len(projects) > MAX_PROJECTS:
            raise ValueError(f"Runner inventory must contain at most {MAX_PROJECTS} projects")
        for name, binding in projects.items():
            identifier(name)
            if not isinstance(binding, str) or len(binding) != 64:
                raise ValueError("Invalid project binding")
        roots = body.get("project_roots", {})
        if not isinstance(roots, dict) or len(roots) > MAX_PROJECTS:
            raise ValueError(f"Runner may advertise at most {MAX_PROJECTS} project roots")
        managed = set()
        for name, root in roots.items():
            identifier(name)
            if (
                not isinstance(root, dict)
                or set(root) != {"binding", "projects"}
                or not isinstance(root["binding"], str)
                or len(root["binding"]) != 64
                or not isinstance(root["projects"], list)
                or len(root["projects"]) > MAX_PROJECTS
            ):
                raise ValueError("Invalid project root inventory")
            for project in root["projects"]:
                identifier(project)
                if project not in projects or project in managed:
                    raise ValueError("Managed projects must belong to one advertised root")
                managed.add(project)
        known_jobs = body.get("known_jobs", [])
        if (
            not isinstance(known_jobs, list)
            or len(known_jobs) > 10000
            or not all(isinstance(j, str) for j in known_jobs)
        ):
            raise ValueError("Invalid Runner job inventory")
        refresh = body.get("refresh_inventory", False)
        if not isinstance(refresh, bool):
            raise ValueError("Invalid inventory refresh flag")
        service.store.register(
            request.state.owner, body["instance"], projects, known_jobs, roots, refresh
        )
        return JSONResponse({"registered": True})

    async def poll(request: Request):
        body = await request.json()
        limit = body.get("limit", 1)
        if type(limit) is not int or not 0 <= limit <= 32:
            raise ValueError("Invalid claim limit")
        deadline = time.monotonic() + 5
        while True:
            jobs = service.claim(request.state.owner, body["instance"], limit)
            if jobs or not limit or time.monotonic() >= deadline:
                return JSONResponse({"jobs": jobs})
            await asyncio.sleep(0.1)

    async def complete(request: Request):
        try:
            body = RunnerCompletion.model_validate(await request.json(), strict=True)
        except ValidationError:
            raise ValueError("Invalid Runner completion result or envelope") from None
        service.store.complete(
            request.state.owner,
            body.job_id,
            body.instance,
            body.status,
            body.result.model_dump(exclude_unset=True),
        )
        return JSONResponse({"accepted": True})

    async def questions(request):
        return JSONResponse(
            {
                "questions": service.store.rows(
                    "SELECT id,task_id,question,created FROM questions WHERE answer IS NULL ORDER BY created LIMIT 100"
                )
            }
        )

    async def answer(request):
        body = await request.json()
        value = body.get("answer")
        if not isinstance(value, str) or not value or len(value) > 16000:
            raise ValueError("Answer must contain 1–16,000 characters")
        with service.store.db:
            row = service.store.one(
                "SELECT * FROM questions WHERE id=?", (request.path_params["question_id"],)
            )
            if not row or row["answer"] is not None:
                raise ValueError("Unanswered question not found")
            service.store.db.execute("UPDATE questions SET answer=? WHERE id=?", (value, row["id"]))
            service.store.event(row["task_id"], "question.answered", {"question_id": row["id"]})
        return JSONResponse({"answered": True})

    class MCPApp:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", health),
            Route("/api/tools", api_tools),
            Route("/api/call", api_call, methods=["POST"]),
            Route("/api/runner/register", register, methods=["POST"]),
            Route("/api/runner/poll", poll, methods=["POST"]),
            Route("/api/runner/complete", complete, methods=["POST"]),
            Route("/api/admin/questions", questions),
            Route("/api/admin/questions/{question_id}", answer, methods=["POST"]),
            Route("/mcp", MCPApp(), methods=["GET", "POST", "DELETE"]),
            Route("/mcp/", MCPApp(), methods=["GET", "POST", "DELETE"]),
        ],
    )
    app.state.service = service
    app.add_middleware(Security, config=config)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    app.add_middleware(RequestDiagnostics, diagnostics=service.diagnostics)
    return app
