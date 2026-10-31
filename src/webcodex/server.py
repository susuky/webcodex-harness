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
from .continuation import ACTIVE as ACTIVE_WAIT_STATES
from .continuation import APP_MIME, APP_URI, Continuations, fingerprint
from .diagnostics import Diagnostics, hash_id
from .management import management_routes
from .projects import PROJECT_OPERATIONS
from .runtime import Runtime
from .store import RunnerCompletion, Store, new_id

TOOL_ACTIVITY = {
    "projects_list": "查詢專案",
    "project_roots_list": "查詢專案位置",
    "project_create": "建立專案",
    "project_register": "登記專案",
    "task_create": "建立任務",
    "work_on_project": "開啟專案工作",
    "task_list": "查詢任務",
    "task_get": "讀取任務進度",
    "task_present": "顯示任務進度",
    "task_review": "讀取修改與結果",
    "task_wait": "監看工作",
    "task_takeover": "在此接手",
    "task_resume": "讀取接續結果",
    "task_update": "更新工作進度",
    "task_events": "讀取工作紀錄",
    "job_get": "讀取工作結果",
    "job_cancel": "取消工作",
    "request_user_input": "提出待確認事項",
    "tool_search": "查詢可用工具",
    "workspace_info": "讀取專案資訊",
    "list_files": "列出檔案",
    "read_file": "讀取檔案",
    "read_instructions": "讀取專案指引",
    "search_files": "搜尋檔案",
    "write_file": "寫入檔案",
    "edit_file": "修改檔案",
    "delete_file": "刪除檔案",
    "apply_patch": "套用檔案修改",
    "git_status": "檢查版本狀態",
    "git_diff": "讀取檔案差異",
    "review_changes": "讀取修改與結果",
    "git_log": "讀取提交紀錄",
    "git_stage": "加入待提交修改",
    "git_commit": "提交修改",
    "exec_command": "執行命令",
    "read_process": "讀取命令結果",
    "write_stdin": "輸入命令資料",
    "cancel_process": "停止命令",
    "resize_terminal": "調整終端畫面",
    "list_processes": "查詢執行中命令",
    "view_image": "檢視圖片",
    "computer_status": "查詢桌面狀態",
    "computer_screenshot": "擷取桌面畫面",
    "computer_move": "移動滑鼠",
    "computer_click": "點擊桌面",
    "computer_scroll": "捲動桌面",
    "computer_drag": "拖曳桌面",
    "computer_key": "按下按鍵",
    "computer_type": "輸入文字",
    "computer_ui_observe": "查找視窗與控制項",
    "computer_ui_control": "操作控制項",
    "fetch_url": "讀取網頁",
    "skills_list": "查詢技能",
    "skill_read": "讀取技能",
    "skill_run": "執行技能工具",
    "extension_install": "安裝擴充功能",
    "extension_remove": "移除擴充功能",
    "plugins_list": "查詢外掛",
    "plugin_tools": "查詢外掛工具",
    "plugin_call": "執行外掛工具",
    "plugin_resources": "查詢外掛資源",
    "plugin_read_resource": "讀取外掛資源",
    "plugin_prompts": "查詢外掛提示",
    "plugin_get_prompt": "讀取外掛提示",
    "plugin_disconnect": "結束外掛連線",
}


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
        """Resume a task using progress: pending jobs and latest process observations/output. Saved status/summary/plan may be stale. Read the handoff and current progress; follow job_get/read_process in this turn, collect the original execution result, and continue authorized work. Task monitoring never requires ending the turn. Later messages require the user to explicitly enable them in the card."""
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
        self.service.route_waiting_progress(task["progress"])
        task["handoff"] = self.service.task_handoff(task, task["progress"])
        return task

    @tool()
    async def task_present(self, task_id: str) -> dict:
        """Show the task progress App once in each user-initiated turn that starts or resumes sustained work. Reuse it while working. It monitors existing jobs; manual continuation and explicitly enabled automatic messages are separate controls. Opening or reconnecting a card keeps automatic messages off. Continue job_get/read_process in the current turn."""
        task = self.task(task_id)
        return self.service.task_presentation(task, self.window)

    @tool()
    async def task_review(
        self,
        task_id: str,
        path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None,
        job_id: str | None = None,
        request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None,
    ) -> dict:
        """Show current changed files (including new files), lazy per-file diffs, and recent real command outcomes/output. Works for active or completed tasks. Reuse request_id after a lost response; use a new request_id to refresh. If review_job is pending, repeat with its exact job_id and path instead of creating another review. Set path to an exact listed file for its preview. Changes cover the current project; execution results record recent command outcomes."""
        task = self.task(task_id)
        if task["kind"] != "coding":
            raise ValueError("Review the coding task created for the registered project")
        if not job_id and request_id:
            previous = self.store.one(
                "SELECT id,tool,arguments FROM jobs WHERE owner=? AND task_id=? AND request_id=?",
                (self.owner, task_id, request_id),
            )
            if previous:
                if (
                    previous["tool"] != "review_changes"
                    or previous["arguments"].get("path") != path
                ):
                    raise ValueError("request_id already belongs to a different review")
                job_id = previous["id"]
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
                    "request_id": request_id,
                },
            )
        return {
            "task_id": task_id,
            "summary": task["summary"],
            "review_job": {key: value for key, value in job.items() if key != "result"},
            "review": job.get("result") if job["status"] == "succeeded" else None,
        }

    @tool("write")
    async def task_wait(
        self,
        task_id: str,
        job_ids: Annotated[list[str], Field(min_length=1, max_length=64)],
        request_id: Annotated[str, Field(min_length=1, max_length=128)],
        replace_wait_id: str | None = None,
    ) -> dict:
        """Explicitly monitor existing jobs in the task card. Use when the user asks to monitor those jobs; continue job_get/read_process in the current turn by default. Monitoring never enables automatic messages. The user can choose manual continuation or explicitly enable limited automatic messages in the card. Reuse request_id for the same jobs. Replace an uncertain notice only after an explicit recovery request."""
        task = self.task(task_id)
        if task["status"] == "completed" or task["kind"] != "coding":
            raise ValueError("Only an unfinished coding task can wait for continuation")
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
        action: Literal[
            "bind",
            "state",
            "prepare",
            "prepare_manual",
            "finish",
            "pause",
            "enable",
            "configure",
            "takeover",
            "unbind",
        ],
        view_id: Annotated[str, Field(pattern="^[0-9a-f]{32}$")],
        generation: Annotated[int, Field(ge=1)] | None = None,
        wait_id: str | None = None,
        outcome: Literal["accepted", "unknown", "not_sent"] | None = None,
        delivery_error: Literal[
            "timeout", "rpc_error", "host_rejected", "interrupted", "invalid_prepare"
        ]
        | None = None,
        delivery_error_code: Annotated[int, Field(ge=-(2**31), lt=2**31)] | None = None,
        max_auto_messages: Annotated[int, Field(ge=0, le=20)] | None = None,
        min_interval_seconds: Annotated[int, Field(ge=10, le=86400)] | None = None,
        request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None,
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
        elif action == "takeover":
            control = continuation.takeover(task_id, view_id, self.window)
            control["review_job"] = await self.service.capture_handoff_review(
                task, control["generation"]
            )
        else:
            if generation is None:
                raise ValueError("The current view generation is required")
            # Verify control before preparing a new snapshot or observing a job.
            continuation.controller(task_id, view_id, generation, self.window)
            if action == "prepare_manual":
                await self.service.sync_card_watch(task, manual=True, request_id=request_id)
            elif action == "prepare":
                await self.service.sync_card_watch(task)
            control = continuation.action(
                task_id,
                view_id,
                generation,
                self.window,
                action,
                wait_id,
                outcome,
                max_auto_messages=max_auto_messages,
                min_interval_seconds=min_interval_seconds,
                request_id=request_id,
            )
        if action in {"bind", "state", "enable"}:
            await self.service.observe_card_processes(task)
            await self.service.sync_card_watch(task)
        return {**self.service.task_presentation(task, self.window), "control": control}

    @tool("write")
    async def task_takeover(self, task_id: str) -> dict:
        """Explicitly take this same account's task into the current conversation. Return its goal, changed files, verification, output cursors, issues and next steps. Keep original commands running and revoke the old card's sending authority. Automatic messaging stays off. Cross-account tasks require a separately authorized identity grant."""
        task = self.task(task_id)
        control = self.service.continuations.takeover(task_id, new_id(), self.window)
        review_job = await self.service.capture_handoff_review(task, control["generation"])
        result = await self.task_get(task_id)
        result["handoff"]["review_job"] = review_job
        result["next_action"] = {"tool": "task_present", "arguments": {"task_id": task_id}}
        return result

    @tool("write")
    async def task_update(
        self,
        task_id: str,
        status: Literal["active", "waiting", "completed"],
        summary: Annotated[str, Field(max_length=32000)],
        plan: Annotated[list[dict[str, str]], Field(max_length=50)],
        handoff: dict[str, list[str]] | None = None,
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
        if handoff is not None:
            if set(handoff) - {"changed_files", "validation", "issues", "next_steps"} or any(
                len(values) > 100 or any(len(value) > 2000 for value in values)
                for values in handoff.values()
            ):
                raise ValueError(
                    "Handoff supports changed_files, validation, issues and next_steps, up to 100 concise entries each"
                )
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO task_handoffs(task_id,snapshot,updated) VALUES (?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET snapshot=excluded.snapshot,updated=excluded.updated",
                    (task_id, json.dumps(handoff, ensure_ascii=False), time.time()),
                )
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
        """Observe the same durable job after a timeout/disconnect. Follow its job_get/read_process next_action in this turn, inspect outcome and exit_code, then continue the authorized work. Do independent work while waiting. Reuse the same job and process instead of repeating the command. A connected task card does not change this waiting strategy."""
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

    async def observe_card_processes(self, task: dict):
        """Refresh bounded live process observations independently of messaging consent."""
        if task["status"] == "completed" or task["kind"] != "coding":
            return
        runner = self.store.one("SELECT seen FROM runners WHERE id=?", (task["runner"],))
        if not runner or time.time() - runner["seen"] >= 15:
            return
        observations = self.store.rows(
            "WITH latest AS (SELECT id,process_id,process_status,updated,"
            "CASE WHEN json_valid(result) THEN coalesce(json_extract(result,'$.next_cursor'),0) ELSE 0 END AS cursor,"
            "row_number() OVER (PARTITION BY process_id ORDER BY updated DESC,id DESC) AS rank "
            "FROM jobs WHERE task_id=? AND status='succeeded' AND process_id IS NOT NULL "
            "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files')) "
            "SELECT id,process_id,cursor FROM latest WHERE rank=1 AND process_status='running' "
            "AND updated<? ORDER BY updated,id LIMIT 2",
            (task["id"], time.time() - 10),
        )
        for observed in observations:
            await self.call(
                task["owner"],
                "read_process",
                {
                    "task_id": task["id"],
                    "process_id": observed["process_id"],
                    "cursor": observed["cursor"],
                    "max_bytes": 8192,
                    "wait_seconds": 0,
                    "request_id": "card-observe:" + observed["id"],
                },
            )

    async def sync_card_watch(
        self, task: dict, *, manual: bool = False, request_id: str | None = None
    ):
        """Observe existing jobs; automatic consent is held only by the active view."""
        if task["status"] == "completed" or task["kind"] != "coding":
            return
        controller = self.store.one("SELECT * FROM task_controllers WHERE task_id=?", (task["id"],))
        automatic = bool(
            controller and controller["auto_enabled"] and controller["lease_until"] > time.time()
        )
        wait = self.continuations.current(task["id"])
        if automatic and (not wait or wait["state"] not in {"prepared", "accepted", "unknown"}):
            # Observation calls are excluded so observing a result cannot create another message.
            roots = self.store.rows(
                "SELECT id FROM jobs WHERE task_id=? "
                "AND tool NOT IN ('read_process','list_processes','workspace_info','review_changes') "
                "AND (created>=? OR status IN ('queued','running') OR (process_status='running' AND "
                "(SELECT latest.process_status FROM jobs latest WHERE latest.task_id=jobs.task_id "
                "AND latest.process_id=jobs.process_id AND latest.status='succeeded' "
                "ORDER BY latest.updated DESC,latest.id DESC LIMIT 1)='running')) "
                "AND NOT EXISTS (SELECT 1 FROM task_watch_members m WHERE m.task_id=jobs.task_id AND m.job_id=jobs.id) "
                "ORDER BY created LIMIT 64",
                (task["id"], controller["enabled_at"]),
            )
            job_ids = [row["id"] for row in roots]
            if job_ids:
                if not wait or wait["state"] not in ACTIVE_WAIT_STATES:
                    wait = self.continuations.arm(
                        task["id"], job_ids, "watch:" + fingerprint(json.dumps(job_ids))
                    )
                self.continuations.append_jobs(task["id"], wait["id"], job_ids)
        if manual and (not wait or wait["state"] not in ACTIVE_WAIT_STATES):
            rows = self.store.rows(
                "SELECT id FROM jobs WHERE task_id=? AND tool NOT IN ('read_process','list_processes','review_changes') "
                "ORDER BY created DESC LIMIT 64",
                (task["id"],),
            )
            ids = [row["id"] for row in rows]
            snapshot = (
                json.dumps([task["id"], request_id])
                if request_id
                else json.dumps([task["id"], task["updated"], ids])
            )
            wait = self.continuations.arm(task["id"], ids, "manual:" + fingerprint(snapshot))
        await self.refresh_task_wait(task)

    def task_continuation(self, task: dict, window: str | None, progress: dict) -> dict:
        continuation = self.continuations.public(task["id"], window)
        wait = continuation["wait"]
        reason = None
        if task["status"] == "completed":
            reason = "completed"
        elif task["kind"] != "coding":
            reason = "unsupported_task"
        elif "write" not in self.config.clients[task["owner"]].permissions:
            reason = "read_only"
        state = "blocked" if reason else wait["state"] if wait else "not_armed"
        handoff = {"state": state, "condition": "explicit_user_request"}
        if reason:
            handoff["reason"] = reason
        return {**continuation, "handoff": handoff}

    @staticmethod
    def route_waiting_job(job: dict):
        # The original observation remains the default in every card state.
        action = job.get("next_action")
        if action and action["tool"] in {"job_get", "read_process"}:
            job["observation_action"] = action
            job["next_step"] = (
                "Continue independent work while this command runs. Follow next_action in this turn "
                "to collect the original result, inspect outcome/exit_code, then continue the task."
            )

    @staticmethod
    def route_waiting_progress(progress: dict):
        actions = progress["next_actions"]
        output = progress["output_actions"]
        if output and progress["state"] not in {"waiting_runner", "project_changed", "completed"}:
            progress["next_actions"] = output + [
                action for action in actions if action not in output
            ]
        progress["observation_actions"] = [
            action
            for action in progress["next_actions"]
            if action["tool"] in {"job_get", "read_process"}
        ]
        if progress["state"] == "observing":
            progress["next_step"] = (
                "Use job_get/read_process in this turn for the existing jobs. Do independent work while "
                "they run, collect their results, and continue the authorized task."
            )
        if progress["recent_issues"] or progress["recent_process_issues"]:
            progress["next_step"] += " Inspect recent_issues and recent_process_issues now."

    async def capture_handoff_review(self, task: dict, generation: int) -> dict:
        review = await self.call(
            task["owner"],
            "review_changes",
            {
                "task_id": task["id"],
                "request_id": f"takeover-review:{generation}",
            },
        )
        return {key: review[key] for key in ("job_id", "status", "next_action") if key in review}

    def task_handoff(self, task: dict, progress: dict, *, include_output: bool = True) -> dict:
        saved = self.store.one(
            "SELECT snapshot,updated FROM task_handoffs WHERE task_id=?", (task["id"],)
        )
        notes = json.loads(saved["snapshot"]) if saved else {}
        files = set(notes.get("changed_files", []))
        reviewed_at = None
        review = (
            self.store.one(
                "SELECT result,updated FROM jobs WHERE task_id=? AND tool='review_changes' "
                "AND status='succeeded' ORDER BY updated DESC LIMIT 1",
                (task["id"],),
            )
            if include_output
            else None
        )
        if review and isinstance(review["result"], dict):
            files.update(
                item["path"]
                for item in review["result"].get("files", [])
                if isinstance(item.get("path"), str)
            )
            reviewed_at = review["result"].get("observed_at", review["updated"])
        for row in (
            self.store.rows(
                "SELECT arguments FROM jobs WHERE task_id=? AND status='succeeded' "
                "AND tool IN ('write_file','edit_file','delete_file','apply_patch') ORDER BY created DESC LIMIT 200",
                (task["id"],),
            )
            if include_output
            else []
        ):
            args = json.loads(row["arguments"])
            if isinstance(args.get("path"), str):
                files.add(args["path"])
            for path in re.findall(r"^\+\+\+ b/(.+)$", args.get("patch", ""), re.M):
                files.add(path)
        outputs = []
        validation = list(notes.get("validation", []))
        for process in progress["process_observations"][:5]:
            observation = self.store.job(process["job_id"], task["owner"]) if include_output else {}
            result = observation.get("result") or {}
            original = (
                self.store.one(
                    "SELECT arguments FROM jobs WHERE id=? AND task_id=?",
                    (process["process_id"], task["id"]),
                )
                if include_output
                else None
            )
            arguments = original["arguments"] if original else {}
            command = arguments.get("command") or arguments.get("argv")
            command = (
                command[:1000]
                if isinstance(command, str)
                else [str(arg)[:300] for arg in command[:6]]
                if isinstance(command, list)
                else None
            )
            outputs.append(
                {
                    "process_id": process["process_id"],
                    "job_id": process["job_id"],
                    "status": process["status"],
                    "exit_code": process["exit_code"],
                    "output": str(result.get("output", ""))[-1200:],
                    "command": command,
                    "next_action": observation.get("next_action"),
                    "output_cursor": observation.get("output_cursor", result.get("next_cursor", 0)),
                    "unread": bool(observation.get("unread_output") or result.get("has_more")),
                    "observed_at": process["observed_at"],
                }
            )
        return {
            "goal": task["title"],
            "summary": task["summary"],
            "plan": task["plan"],
            "changed_files": sorted(files)[:100],
            "validation": validation,
            "unread_outputs": outputs,
            "issues": notes.get("issues", [])
            + progress["recent_issues"]
            + progress["recent_process_issues"],
            "next_steps": notes.get("next_steps", [])
            or [p["step"] for p in task["plan"] if p["status"] != "completed"],
            "next_actions": progress["next_actions"],
            "updated_at": saved["updated"] if saved else task["updated"],
            "files_observed_at": reviewed_at,
        }

    def task_presentation(self, task: dict, window: str | None = None) -> dict:
        progress = self.task_progress(task, window, include_output=False)
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
                "last_job_update": progress["last_job_update"],
                "next_step": progress["next_step"],
                "liveness": progress["liveness"],
            },
            "continuation": continuation,
            "handoff": self.task_handoff(task, progress, include_output=False),
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

    def task_progress(
        self, task: dict, window: str | None = None, *, include_output: bool = True
    ) -> dict:
        activity = self.store.task_activity(task, include_output=include_output)
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
        if work["running_count"] > len(running):
            # Keep an old live command observable when newer completed commands fill the card.
            older = self.store.rows(
                "WITH latest AS (SELECT id AS job_id,process_id,process_status AS status,"
                "row_number() OVER (PARTITION BY process_id ORDER BY updated DESC,id DESC) AS rank "
                "FROM jobs WHERE task_id=? AND status='succeeded' AND process_id IS NOT NULL "
                "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files')) "
                "SELECT job_id,process_id,status FROM latest WHERE rank=1 AND status='running'",
                (task["id"],),
            )
            visible = {p["process_id"] for p in running}
            for process in older:
                if process["process_id"] not in visible:
                    observation = self.store.job(process["job_id"], task["owner"])
                    if observation.get("next_action"):
                        actions.append(observation["next_action"])
        output_actions = []
        for process in activity["process_observations"] if include_output else []:
            observation = self.store.job(process["job_id"], task["owner"])
            action = observation.get("next_action")
            if action and (
                (observation.get("result") or {}).get("has_more")
                or observation.get("unread_output")
            ):
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
        elif (pending or work["running_count"]) and not online:
            state, message = (
                "waiting_runner",
                "The Runner has no recent heartbeat. Preserve these jobs and wait for the Runner.",
            )
        elif questions:
            state, message = (
                "waiting_operator",
                "An out-of-band operator question is unanswered. Ordinary clarification belongs in the current conversation.",
            )
        elif pending or work["running_count"]:
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
        conversation = self.store.tool_activity(task["id"], window)
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
                "host_turn_state": "unknown",
                "data_freshness": "live" if online else "historical",
                "conversation": conversation,
                "operation": TOOL_ACTIVITY.get(conversation["tool"], "執行工具")
                if conversation
                else None,
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
            if spec.name != "task_app":
                label = TOOL_ACTIVITY.get(spec.name, "執行工具")
                spec.title = label
                spec.meta.update(
                    {
                        "openai/toolInvocation/invoking": label + "…",
                        "openai/toolInvocation/invoked": label + "：已收到回應",
                    }
                )
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
        if path == "/healthz" or (path == "/manage" and scope["method"] == "GET"):
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
            "You can work on repositories through this coding harness. Prefer dedicated connectors or APIs "
            "when they can complete the task. For website UI work, prefer the client's native browser tools "
            "on the target computer (Chrome for existing signed-in tabs, or its built-in browser). Keep "
            "browser work in background tabs without moving the human's pointer or switching their active tab. "
            "If native browser tools are unavailable, including in a ChatGPT web conversation, use the "
            "installed Playwright browser-tools plugin: plugins_list, plugin_tools(plugin='browser-tools', "
            "server='browser'), then plugin_call with the returned schemas. Its headless browser runs on "
            "the task's Runner with a separate login session. computer_* controls the shared desktop; use it "
            "only for native applications, OS dialogs, or a necessary step that available browser tools "
            "cannot perform. Return to browser tools after that step. A denied website or tool permission "
            "must not be bypassed through desktop control. For existing projects, use projects_list, "
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
            "Continue authorized work in the same turn. Keep job_get/read_process as the default "
            "when waiting for existing commands; do independent work while they run and then collect "
            "their result. A connected task card never changes this default. Do not end the turn just "
            "because only waiting remains, and do not ask for repeated continue messages. "
            "Tell the user what is starting before a long operation. While working, give a brief visible "
            "update when the state changes or after about 30 seconds of waiting; don't silently chain "
            "tool calls. Base updates on observed results, not estimated completion percentages. "
            "In each user-initiated turn that starts or resumes sustained work, call task_present "
            "once to show the task progress App, even if an earlier turn already showed it. "
            "Follow work_on_project.next_action to present it. Reuse that card for ordinary tool "
            "calls within the turn and for automatic task_resume continuation. "
            "Task monitoring and message delivery are independent. task_wait is an explicit monitoring "
            "choice and never enables messages or requires yielding. Automatic messages default off; "
            "only the user's explicit card choice enables bounded delivery. Reconnects and task takeover "
            "reset that choice. Manual continuation uses one idempotent delivery. On a continuation "
            "message call task_resume first; already_consumed=true ends the duplicate workflow. "
            "For an explicit cross-conversation takeover, use task_takeover and read its handoff; "
            "continue original jobs and revoke old card control. Runner connectivity and last tool "
            "activity are observations; Host model turn status is unknown. "
            "Ask ordinary clarification directly in the conversation; request_user_input records an "
            "out-of-band CLI question and is not the ChatGPT question UI. "
            "Reasoning and any subagent orchestration stay in ChatGPT or the MCP client. "
            "WebCodex supplies host tools only. Every conversation is prohibited from starting "
            "Codex or Work agents/workers through WebCodex. Never use exec_command, write_stdin, "
            "skill scripts, plugins, MCP providers, SDKs or HTTP APIs to launch or delegate to them. "
            "There is no opt-in setting or fallback worker backend; keep any permitted native "
            "collaboration inside the host client. Do not install a worker provider to bypass this rule. "
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
                        message=f"{TOOL_ACTIVITY.get(name, '執行工具')}，已等候 {int(elapsed)} 秒。",
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
            *management_routes(service),
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
