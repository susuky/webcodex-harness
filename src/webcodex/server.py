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
from .connections import ConnectionHistory
from .continuation import ACTIVE as ACTIVE_WAIT_STATES
from .continuation import APP_MIME, APP_URI, Continuations, fingerprint
from .diagnostics import HTTP_ROUTES, ConnectionMonitor, Diagnostics, hash_id
from .management import Management, management_routes
from .management_auth import PUBLIC_ROUTES, ManagementAuth
from .management_sources import Sources
from .projects import PROJECT_OPERATIONS
from .runtime import Runtime
from .store import (
    INTERNAL_OBSERVATION_PREFIXES,
    RunnerCompletion,
    Store,
    TaskArchived,
    new_id,
    require_unarchived,
)

TOOL_ACTIVITY = {
    "projects_list": "查詢專案",
    "connection_status": "查詢連線紀錄",
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
    "view_images": "預覽多張圖片",
    "import_image": "匯入圖片",
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
    async def connection_status(self) -> dict:
        """Read current Server, source and Runner connection state and saved outage reasons, including recovery times. Works without dispatching Runner jobs; use it when a Runner or source is unavailable."""
        return await self.service.sources.connection_data(self.owner, include_evidence=True)

    @tool()
    async def projects_list(self) -> dict:
        """List authorized projects and Runner heartbeat status. Host errors like 'This conversation does not support developer MCPs' leave Runner status unknown. Start coding with an exact returned project ID, or use project_roots_list if empty."""
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
        """List allowed parent roots for project_create/project_register and Runner heartbeat status."""
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
        """Create a durable coding task for an authorized project ID; save task_id for task_takeover later."""
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
        """Start coding on project (or load existing task_id workspace). Reuse request_id on retries; follow next_action for task_present and workspace.next_action for Git/instructions. Use task_takeover for cross-conversation handoff."""
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
        require_unarchived(task)
        workspace = await self.service.call(
            self.owner, "workspace_info", {"task_id": task["id"]}, window=self.window
        )
        task = self.task(task["id"])
        require_unarchived(task)
        return {
            "task": self.task_snapshot(task),
            "workspace": workspace,
            "next_action": {"tool": "task_present", "arguments": {"task_id": task["id"]}},
        }

    @tool()
    async def task_list(
        self,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
        include_archived: bool = False,
    ) -> dict:
        """List this client's durable task IDs (pass include_archived=true to include archived tasks)."""
        rows, next_offset = self.service.list_task_page(
            self.owner, offset, limit, include_archived=include_archived
        )
        return {
            "tasks": [
                {
                    **r,
                    "next_action": {"tool": "task_get", "arguments": {"task_id": r["id"]}},
                }
                for r in rows
            ],
            "next_offset": next_offset,
        }

    @tool()
    async def task_get(self, task_id: str) -> dict:
        """Read recent task history and progress. Use task_takeover directly when taking over in this conversation."""
        task = self.task(task_id)
        task["jobs"] = self.store.rows(
            "SELECT id AS job_id,tool,status,created,updated FROM jobs WHERE task_id=? ORDER BY created DESC LIMIT 30",
            (task_id,),
        )
        task["questions"] = self.store.rows(
            "SELECT id,question,answer FROM questions WHERE task_id=? "
            "ORDER BY answer IS NOT NULL, created DESC LIMIT 10",
            (task_id,),
        )
        return self.task_snapshot(task, handoff=True)

    def task_snapshot(self, task: dict, *, handoff: bool = False) -> dict:
        handoff_jobs = {} if handoff else None
        progress = self.service.task_progress(task, self.window, handoff_jobs=handoff_jobs)
        self.service.route_waiting_progress(progress)
        result = {
            **task,
            "progress": progress,
            "service": self.store.service,
            "continuation": self.service.task_continuation(task, self.window),
        }
        if handoff:
            result["handoff"] = self.service.task_handoff(task, progress, observations=handoff_jobs)
        return result

    @tool()
    async def task_present(self, task_id: str, include_handoff: bool = True) -> dict:
        """Show the task progress App once per user-initiated turn. Opening the card keeps automatic messaging off. Keep calling job_get/read_process in the same turn until work finishes; do not end the turn just because a card is open or only waiting remains."""
        task = self.task(task_id)
        return self.service.task_presentation(task, self.window, include_handoff=include_handoff)

    @tool()
    async def task_review(
        self,
        task_id: str,
        path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None,
        job_id: str | None = None,
        request_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None,
    ) -> dict:
        """Review changed/untracked files, per-file diff (when path is set), and recent command results. Reuse request_id on retries or poll job_id if pending."""
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
        """Explicitly monitor existing jobs in the task card without enabling automatic messages. Keep calling job_get/read_process in the same turn until work finishes; do not end the turn just because a card is open or only waiting remains."""
        task = self.task(task_id)
        require_unarchived(task)
        if task["status"] == "completed" or task["kind"] != "coding":
            raise ValueError("Only an unfinished coding task can wait for continuation")
        for job_id in job_ids:
            if self.store.job(job_id, self.owner)["task_id"] != task_id:
                raise ValueError("Every waiting job must belong to this exact task")
        self.service.continuations.arm(task_id, job_ids, request_id, replace_wait_id)
        await self.service.refresh_task_wait(task)
        return self.service.task_presentation(self.task(task_id), self.window)

    @tool("write")
    async def task_resume(
        self,
        task_id: str,
        wait_id: str,
        consume_token: Annotated[str, Field(min_length=32, max_length=128)],
    ) -> dict:
        """Consume an App continuation wake idempotently before resuming work (stop if already_consumed=true)."""
        task = self.task(task_id)
        require_unarchived(task)
        if task["status"] == "completed":
            raise ValueError("This task is already completed")
        consumed = self.service.continuations.consume(task_id, wait_id, consume_token)
        if consumed["already_consumed"]:
            return consumed
        return {**consumed, "task": self.task_snapshot(task)}

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
        """App-only coordination for the task view (cannot execute commands, approve jobs, or consume wakes)."""
        task = self.task(task_id)
        if action != "finish" and (delivery_error is not None or delivery_error_code is not None):
            raise ValueError("Delivery diagnostics belong to the finish action")
        if task["archived_at"] is not None and action in {"bind", "state"}:
            return {
                **self.service.task_presentation(task, self.window),
                "control": {"bound": False, "reason": "archived"},
            }
        require_unarchived(task)
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
            if action in {"prepare_manual", "prepare"}:
                await self.service.sync_card_watch(
                    task, manual=action == "prepare_manual", request_id=request_id
                )
                task = self.task(task_id)
                require_unarchived(task)
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
            try:
                await self.service.observe_card_processes(task)
                await self.service.sync_card_watch(task)
            except TaskArchived:
                pass  # An archive can arrive while waiting for an in-flight observation.
        if action in {"bind", "state", "enable", "takeover"}:
            task = self.task(task_id)
        if task["archived_at"] is not None:
            control = {"bound": False, "reason": "archived"}
        return {**self.service.task_presentation(task, self.window), "control": control}

    @tool("write")
    async def task_takeover(self, task_id: str) -> dict:
        """Take over a task into this conversation, returning progress and handoff while preserving running jobs/processes."""
        task = self.task(task_id)
        require_unarchived(task)
        control = self.service.continuations.takeover(task_id, new_id(), self.window)
        review_job = await self.service.capture_handoff_review(task, control["generation"])
        task = self.task(task_id)
        require_unarchived(task)
        result = self.task_snapshot(task, handoff=True)
        result["handoff"]["review_job"] = review_job
        result["next_action"] = {
            "tool": "task_present",
            "arguments": {"task_id": task_id, "include_handoff": False},
        }
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
        """Save task status, summary (in user's language, Markdown bullets/paths), plan ({step, status}), and optional handoff."""
        task = self.task(task_id)
        require_unarchived(task)
        if task["kind"] != "coding":
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
        """Read task audit events after a numeric cursor."""
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
        self, job_id: str, wait_seconds: Annotated[float, Field(ge=0, le=30)] = 0
    ) -> dict:
        """Observe an existing durable job. Keep calling job_get/read_process (with long wait_seconds, e.g. 20–30s) in the same turn until finished; do not end the turn or ask the user to continue just because only waiting remains. Do independent work while waiting and reuse the same job/process instead of repeating commands."""
        job = self.store.job(job_id, self.owner)
        task = self.task(job["task_id"])
        deadline = time.monotonic() + wait_seconds
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            if job["status"] == "queued" and task["archived_at"] is not None:
                break
            await self.service.wait_job_event(job_id, min(0.5, max(0, deadline - time.monotonic())))
            job = self.store.job(job_id, self.owner)
            task = self.task(job["task_id"])
        if task["archived_at"] is not None:
            job.pop("next_action", None)
            job["next_step"] = "This task is archived. Restore it in the task center to continue."
        self.service.route_waiting_job(job)
        return job

    @tool("write")
    async def job_cancel(self, job_id: str) -> dict:
        """Cancel a queued job; use cancel_process once a process_id exists."""
        job = self.store.job(job_id, self.owner)
        self.task(job["task_id"])
        return self.store.cancel(job_id, self.owner)

    @tool("write")
    async def request_user_input(
        self, task_id: str, question: Annotated[str, Field(min_length=1, max_length=4000)]
    ) -> dict:
        """Record an out-of-band CLI question (for live chat users, ask directly in conversation)."""
        require_unarchived(self.task(task_id))
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
        """Search built-in tool schemas by name or purpose."""
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
        self.connections = ConnectionHistory(config.state_dir / "connections")
        self.observe_tool_activity(self.store.interrupt_tool_activity)
        self.continuations = Continuations(self.store)
        self._job_waiters: dict[str, set[asyncio.Event]] = {}
        self._runner_waiters: dict[str, set[asyncio.Event]] = {}
        self.store._on_enqueue = self.notify_runner
        self.store._on_job_change = self.notify_job
        self.resume_legacy_jobs()

    def notify_job(self, job_id: str):
        for event in list(self._job_waiters.get(job_id, ())):
            event.set()

    def notify_runner(self, runner: str):
        for event in list(self._runner_waiters.get(runner, ())):
            event.set()

    async def _wait_event(
        self, registry: dict[str, set[asyncio.Event]], key: str, wait_seconds: float
    ):
        if wait_seconds <= 0:
            return
        event = asyncio.Event()
        waiters = registry.setdefault(key, set())
        waiters.add(event)
        try:
            async with asyncio.timeout(wait_seconds):
                await event.wait()
        except TimeoutError:
            pass
        finally:
            waiters.discard(event)
            if not waiters:
                registry.pop(key, None)

    async def wait_job_event(self, job_id: str, wait_seconds: float):
        await self._wait_event(self._job_waiters, job_id, wait_seconds)

    async def wait_runner_event(self, runner: str, wait_seconds: float):
        await self._wait_event(self._runner_waiters, runner, wait_seconds)

    def set_task_archive(self, owner: str, task_id: str, archived_at: float | None):
        notify_jobs: list[str] = []
        notify_runner_name: str | None = None
        with self.continuations.transaction():
            changed = self.store.db.execute(
                "UPDATE tasks SET archived_at=? WHERE owner=? AND id=? "
                "AND (archived_at IS NOT NULL) != ?",
                (archived_at, owner, task_id, archived_at is not None),
            )
            if changed.rowcount:
                if archived_at is not None:
                    self.continuations.cancel_pending(task_id)
                    self.store.db.execute(
                        "UPDATE task_controllers SET lease_until=0 WHERE task_id=?", (task_id,)
                    )
                    notify_jobs = [
                        row["id"]
                        for row in self.store.rows(
                            "SELECT id FROM jobs WHERE owner=? AND task_id=? "
                            "AND status IN ('queued','running')",
                            (owner, task_id),
                        )
                    ]
                else:
                    row = self.store.one(
                        "SELECT runner FROM tasks WHERE owner=? AND id=?", (owner, task_id)
                    )
                    notify_runner_name = row["runner"] if row else None
                self.store.event(
                    task_id, "task.archived" if archived_at is not None else "task.restored", {}
                )
        for job_id in notify_jobs:
            self.notify_job(job_id)
        if notify_runner_name:
            self.notify_runner(notify_runner_name)

    async def refresh_task_wait(self, task: dict):
        if task["archived_at"] is not None:
            return
        wait = self.continuations.current(task["id"])
        if not wait or wait["state"] != "watching":
            return
        if task["status"] == "completed":
            self.continuations.cancel(task["id"])
            return
        jobs, ready = [], True
        watch_request = "watch:" + wait["id"] + ":"
        for job_id in json.loads(wait["jobs"]):
            job = self.store.job(job_id, task["owner"])
            result = job.get("result") or {}
            process_id = None
            cursor = 0
            max_bytes = 8192
            if (
                job["status"] == "succeeded"
                and result.get("status") == "running"
                and result.get("process_id")
            ):
                process_id = result["process_id"]
                if isinstance(result.get("next_cursor"), int):
                    cursor = max(0, result["next_cursor"])
            elif (
                job["status"] == "unknown"
                and job["tool"] == "read_process"
                and (job["request_id"] or "").startswith(watch_request)
            ):
                arguments = self.store.one("SELECT arguments FROM jobs WHERE id=?", (job_id,))
                process_id = arguments["arguments"]["process_id"]
                cursor = arguments["arguments"].get("cursor", 0)
                max_bytes = arguments["arguments"].get("max_bytes", 8192)
            if process_id:
                request_id = watch_request + job_id
                existing = self.store.one(
                    "SELECT id FROM jobs WHERE owner=? AND task_id=? AND request_id=? "
                    "AND tool='read_process' AND json_extract(arguments,'$.process_id')=?",
                    (task["owner"], task["id"], request_id, process_id),
                )
                if existing:
                    job = self.store.job(existing["id"], task["owner"])
                else:
                    job = await self.call(
                        task["owner"],
                        "read_process",
                        {
                            "task_id": task["id"],
                            "process_id": process_id,
                            "cursor": cursor,
                            "max_bytes": max_bytes,
                            "wait_seconds": 0,
                            "request_id": request_id,
                        },
                    )
            jobs.append(job["job_id"])
            if (
                job["status"] in {"queued", "running", "pending_approval"}
                or (job["status"] == "unknown" and process_id)
                or (
                    job["status"] == "succeeded"
                    and (job.get("result") or {}).get("status") == "running"
                )
            ):
                ready = False
        self.continuations.refresh(wait["id"], wait["jobs"], jobs, ready)

    async def request_process_observation(
        self,
        owner: str,
        task_id: str,
        process_id: str,
        previous_job_id: str,
        *,
        request_prefix: str,
        max_bytes: int,
    ) -> dict:
        request_id = request_prefix + previous_job_id
        attempt = self.store.one(
            "SELECT id AS job_id,status,updated FROM jobs WHERE owner=? AND task_id=? "
            "AND tool='read_process' AND (request_id=? OR request_id GLOB ?) "
            "ORDER BY rowid DESC LIMIT 1",
            (owner, task_id, request_id, request_id + ":*"),
        )
        if attempt:
            if (
                attempt["status"] not in {"failed", "unknown"}
                or time.time() - attempt["updated"] < 10
            ):
                return attempt
            request_id += ":" + attempt["job_id"]
        cursor = self.store.one(
            "SELECT CASE WHEN json_valid(result) THEN CASE "
            "WHEN json_type(result,'$.next_cursor')='integer' "
            "THEN max(0,json_extract(result,'$.next_cursor')) ELSE 0 END ELSE 0 END AS cursor "
            "FROM jobs WHERE id=?",
            (previous_job_id,),
        )["cursor"]
        return await self.call(
            owner,
            "read_process",
            {
                "task_id": task_id,
                "process_id": process_id,
                "cursor": cursor,
                "max_bytes": max_bytes,
                "wait_seconds": 0,
                "request_id": request_id,
            },
        )

    async def observe_card_processes(self, task: dict):
        """Refresh bounded live process observations independently of messaging consent."""
        if (
            task["archived_at"] is not None
            or task["status"] == "completed"
            or task["kind"] != "coding"
        ):
            return
        runner = self.store.one("SELECT seen FROM runners WHERE id=?", (task["runner"],))
        if not runner or time.time() - runner["seen"] >= 15:
            return
        observations = self.store.rows(
            "SELECT latest.id,state.process_id "
            "FROM process_states state JOIN jobs latest ON latest.id=("
            "SELECT id FROM jobs INDEXED BY process_observations "
            "WHERE task_id=state.task_id AND process_id=state.process_id AND status='succeeded' "
            "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files') "
            "ORDER BY updated DESC,id DESC LIMIT 1) "
            "WHERE state.task_id=? AND state.status='running' AND latest.updated<? "
            "ORDER BY latest.updated,latest.id LIMIT 2",
            (task["id"], time.time() - 10),
        )
        for observed in observations:
            await self.request_process_observation(
                task["owner"],
                task["id"],
                observed["process_id"],
                observed["id"],
                request_prefix="card-observe:",
                max_bytes=8192,
            )

    async def sync_card_watch(
        self, task: dict, *, manual: bool = False, request_id: str | None = None
    ):
        """Observe existing jobs; automatic consent is held only by the active view."""
        if (
            task["archived_at"] is not None
            or task["status"] == "completed"
            or task["kind"] != "coding"
        ):
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
                "EXISTS (SELECT 1 FROM process_states WHERE task_id=jobs.task_id "
                "AND process_id=jobs.process_id AND status='running'))) "
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

    def task_continuation(
        self, task: dict, window: str | None, *, include_deliveries: bool = False
    ) -> dict:
        continuation = self.continuations.public(
            task["id"], window, include_deliveries=include_deliveries
        )
        wait = continuation["wait"]
        reason = None
        if task["archived_at"] is not None:
            reason = "archived"
        elif task["status"] == "completed":
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
                "Keep calling next_action (job_get/read_process) in this turn until finished; do not end the turn or ask the user to continue while waiting. "
                "Do independent work while this runs, then inspect outcome/exit_code and continue."
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
                "Keep calling job_get/read_process in this turn until existing jobs finish; do not end the turn while waiting. "
                "Do independent work while they run, collect their results, and continue."
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

    def task_handoff(
        self,
        task: dict,
        progress: dict,
        *,
        include_output: bool = True,
        observations: dict[str, dict] | None = None,
    ) -> dict:
        saved = self.store.one(
            "SELECT snapshot,updated FROM task_handoffs WHERE task_id=?", (task["id"],)
        )
        notes = json.loads(saved["snapshot"]) if saved else {}
        files = set(notes.get("changed_files", []))
        reviewed_at = None
        review = (
            self.store.one(
                "SELECT result,updated FROM jobs INDEXED BY task_job_history "
                "WHERE task_id=? AND tool='review_changes' "
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
                "SELECT tool, json_extract(arguments, '$.path') AS path, "
                "CASE WHEN tool='apply_patch' THEN json_extract(arguments, '$.patch') ELSE NULL END AS patch "
                "FROM jobs INDEXED BY task_job_history "
                "WHERE task_id=? AND status='succeeded' "
                "AND tool IN ('write_file','edit_file','delete_file','apply_patch','import_image') "
                "ORDER BY created DESC LIMIT 200",
                (task["id"],),
            )
            if include_output
            else []
        ):
            if isinstance(row["path"], str):
                files.add(row["path"])
            if isinstance(row["patch"], str):
                for path in re.findall(r"^\+\+\+ b/(.+)$", row["patch"], re.M):
                    files.add(path)
        outputs = []
        validation = list(notes.get("validation", []))
        for process in progress["process_observations"][:5]:
            observation = {}
            if include_output:
                observation = (observations or {}).get(process["job_id"]) or self.store.job(
                    process["job_id"], task["owner"]
                )
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
                    "next_action": observation.get("next_action")
                    if task["archived_at"] is None
                    else None,
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

    def task_presentation(
        self, task: dict, window: str | None = None, *, include_handoff: bool = True
    ) -> dict:
        progress = self.task_progress(task, window, include_output=False)
        continuation = self.task_continuation(task, window, include_deliveries=True)
        result = {
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
        }
        if include_handoff:
            result["handoff"] = self.task_handoff(task, progress, include_output=False)
        return result

    def resume_legacy_jobs(self):
        # Upgrade old queues in place; revoked access and completed tasks never execute.
        with self.store.db:
            for job in self.store.rows(
                "SELECT jobs.id,jobs.task_id,jobs.owner,jobs.runner,jobs.project,jobs.tool,"
                "tasks.kind,tasks.status AS task_status FROM jobs INDEXED BY pending_jobs "
                "JOIN tasks ON tasks.id=jobs.task_id "
                "WHERE jobs.status IN ('queued','pending_approval','running') "
                "AND jobs.status='pending_approval'"
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

    def list_task_page(
        self,
        owner: str,
        offset: int,
        limit: int,
        *,
        include_archived: bool,
        catalog: bool = False,
    ) -> tuple[list[dict], int | None]:
        allowed = sorted(self.allowed_projects(owner))
        roots = self.config.clients[owner].project_roots
        project_slots = ",".join("?" for _ in allowed) or "NULL"
        root_slots = ",".join("?" for _ in roots) or "NULL"
        # Catalog pages are sorted by the task center after collection. Updates must not shift them.
        order = "rowid" if catalog else "updated DESC,rowid DESC"
        rows = self.store.rows(
            "SELECT id,title,runner,project,kind,status,summary,updated,archived_at FROM tasks WHERE owner=? "
            "AND (? OR archived_at IS NULL) "
            f"AND ((kind='coding' AND runner||'/'||project IN ({project_slots})) "
            f"OR (kind='project' AND runner||'/'||project IN ({root_slots}))) "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (owner, include_archived, *allowed, *roots, limit + 1, offset),
        )
        return rows[:limit], offset + limit if len(rows) > limit else None

    def check_project(self, owner: str, project: str, kind: str = "coding"):
        allowed = (
            self.config.clients[owner].project_roots
            if kind == "project"
            else self.allowed_projects(owner)
        )
        if project not in allowed:
            raise ValueError("Project is not authorized for this client")

    def task_progress(
        self,
        task: dict,
        window: str | None = None,
        *,
        include_output: bool = True,
        handoff_jobs: dict[str, dict] | None = None,
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
            older = self.store.running_processes(task["id"])
            visible = {p["process_id"] for p in running}
            for process in older:
                if process["process_id"] not in visible:
                    observation = self.store.job(process["job_id"], task["owner"])
                    if observation.get("next_action"):
                        actions.append(observation["next_action"])
        observations = activity["process_observations"] if include_output else []
        process_meta = {}
        if len(observations) > 1:
            ids = [process["job_id"] for process in observations[1:]]
            placeholders = ",".join("?" for _ in ids)
            for row in self.store.rows(
                f"SELECT id, request_id, "
                f"CASE WHEN json_valid(result) AND json_extract(result,'$.has_more') THEN 1 ELSE 0 END AS has_more "
                f"FROM jobs WHERE id IN ({placeholders})",
                tuple(ids),
            ):
                process_meta[row["id"]] = row
        output_actions = []
        for index, process in enumerate(observations):
            meta = process_meta.get(process["job_id"])
            if index == 0:
                observation = activity["latest_execution"]
            elif (
                index < 5
                or process["status"] == "running"
                or (meta and meta["has_more"])
                or (meta and (meta["request_id"] or "").startswith(INTERNAL_OBSERVATION_PREFIXES))
            ):
                observation = self.store.job(process["job_id"], task["owner"])
            else:
                observation = {}
            if handoff_jobs is not None and index < 5:
                handoff_jobs[process["job_id"]] = observation
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
            "SELECT id AS job_id,tool,status FROM jobs INDEXED BY task_jobs "
            "WHERE task_id=? AND updated>? "
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
        if task["archived_at"] is not None:
            state, message = (
                "archived",
                "This task is archived. Restore it in the task center to continue.",
            )
        elif binding != task["binding"]:
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
        conversation = self.store.tool_activity(task["id"], window) if window else None
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
            if state in {"waiting_runner", "project_changed", "completed", "archived"}
            else actions,
            "output_actions": [] if state == "archived" else output_actions,
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
            if spec.name == "import_image":
                spec.meta["openai/fileParams"] = ["file"]
                spec.inputSchema["properties"]["file"] = spec.inputSchema.pop("$defs")["ImageFile"]
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
                job = self.store.one(
                    "SELECT task_id FROM jobs WHERE id=? AND owner=?", (job_id, owner)
                )
                task_id = job["task_id"] if job else None
            elif "task_id" in definition.model.model_fields:
                task_id = arguments.get("task_id")
            else:
                return None
            if isinstance(task_id, str):
                task = TaskTools(self, owner).task(task_id)
                if task["archived_at"] is None:
                    return task["id"]
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
        self,
        owner: str,
        name: str,
        arguments: dict,
        *,
        window: str | None = None,
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
        if name == "exec_command" and args["timeout_seconds"] is None:
            # Older Runners accept an omitted timeout but reject null.
            del args["timeout_seconds"]
        job = self.store.enqueue(
            task,
            name,
            args,
            request_id,
            self.config.max_pending_jobs,
            allow_completed=definition.permission == "read",
            defaulted_arguments=frozenset(args.keys() - arguments.keys()),
        )
        if (request_id or "").startswith((*INTERNAL_OBSERVATION_PREFIXES, "takeover-review:")):
            return await local.job_get(job["job_id"], wait_seconds=0.5)
        runner = self.store.one("SELECT seen FROM runners WHERE id=?", (task["runner"],))
        if not runner or time.time() - runner["seen"] >= 15:
            return await local.job_get(job["job_id"], wait_seconds=0)
        claim_deadline = time.monotonic() + 1.0
        while job["status"] == "queued" and time.monotonic() < claim_deadline:
            if local.task(task["id"])["archived_at"] is not None:
                break
            await self.wait_job_event(
                job["job_id"], min(0.25, max(0, claim_deadline - time.monotonic()))
            )
            job = self.store.job(job["job_id"], owner)
        if job["status"] == "queued":
            return await local.job_get(job["job_id"], wait_seconds=0)
        if job["status"] == "running":
            if name == "read_process":
                phase2 = min(31.0, float(args.get("wait_seconds") or 0) + 0.5)
            elif name == "exec_command":
                phase2 = min(22.0, max(5.0, (args.get("yield_time_ms", 2000) / 1000.0) + 2.0))
            else:
                phase2 = 15.0
            return await local.job_get(job["job_id"], wait_seconds=phase2)
        return await local.job_get(job["job_id"], wait_seconds=0)


def mcp_result(result: dict) -> types.CallToolResult:
    native = (result.get("result") or {}).get("mcp_result")
    content = []
    error = result.get("status") in {"failed", "cancelled", "unknown"}
    if native:
        converted = types.CallToolResult.model_validate(native)
        error = error or converted.isError
        result = dict(result)
        result["result"] = {k: v for k, v in result["result"].items() if k != "mcp_result"}
        provider = converted.structuredContent
        if provider is not None:
            result["result"]["provider_result"] = provider
        for item in converted.content:
            if (
                provider is not None
                and isinstance(item, types.TextContent)
                and item.text.lstrip().startswith("{")
            ):
                try:
                    if json.loads(item.text) == provider:
                        continue
                except (json.JSONDecodeError, RecursionError):
                    pass
            content.append(item)
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
        source_id = None
        if path == "/api/manage":
            route = path
        elif path.startswith("/api/manage/"):
            parts = path.split("/")
            if len(parts) > 4 and parts[3] == "sources":
                if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", parts[4]):
                    source_id = parts[4]
                parts[4] = ":source_id"
                if len(parts) > 6 and parts[5] == "tasks":
                    parts[6] = ":id"
            elif len(parts) > 4 and parts[3] == "tasks":
                parts[4] = ":id"
            candidate = "/".join(parts)
            if candidate in HTTP_ROUTES:
                route = candidate
        http_id = new_id()
        scope.setdefault("state", {})["http_id"] = http_id
        fields = {"http_id": http_id, "route": route, "method": scope["method"]}
        if source_id is not None:
            fields["source_id"] = source_id
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
            duration_ms = (time.monotonic() - started) * 1000
            slow_after = 6000 if route == "/api/runner/poll" else 1000
            if not quiet or event != "http.completed" or status != 200 or duration_ms >= slow_after:
                if route == "/api/manage/sources/:source_id/state":
                    fields.update(
                        source_loopback_port=scope["state"].get("source_loopback_port"),
                        source_transport_error=scope["state"].get("source_transport_error"),
                    )
                self.diagnostics.record(
                    event,
                    **fields,
                    http_status=status,
                    response_bytes=size,
                    disconnected=disconnected,
                    duration_ms=duration_ms,
                )


class Security:
    def __init__(self, app, config: ServerConfig, management_auth: ManagementAuth):
        self.app, self.config = app, config
        self.management_auth = management_auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path == "/healthz" or (path == "/manage" and scope["method"] == "GET"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        header = headers.get(b"authorization", b"").decode("latin-1")
        token = header[7:] if header.startswith("Bearer ") else ""
        management = path.startswith("/api/manage/")
        public_login = (path, scope["method"]) in PUBLIC_ROUTES
        session_owner = self.management_auth.owner(scope) if management else None
        owner = session_owner
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
        elif owner is None:
            owner = next(
                (
                    name
                    for name, client in self.config.clients.items()
                    if hmac.compare_digest(client.token.encode(), token.encode())
                ),
                None,
            )
        if owner is None and not public_login:
            return await JSONResponse(
                {"error": "Unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        origin = headers.get(b"origin", b"").decode("latin-1")
        if (public_login or session_owner) and scope["method"] != "GET" and not origin:
            return await JSONResponse({"error": "Origin is required"}, 403)(scope, receive, send)
        if origin:
            allowed = (
                self.management_auth.allowed_origin(origin)
                if management
                else origin == self.config.public_url
            )
            if not allowed:
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
        if path.rstrip("/") == "/mcp" and scope["method"] == "POST":
            try:
                request = types.JSONRPCRequest.model_validate_json(
                    b"".join(message.get("body", b"") for message in messages), strict=True
                )
            except ValidationError:
                request = None
            # Fall back before the SDK rejects the discovery probe's newer protocol version.
            if request is not None and request.method == "server/discover":
                return await JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request.id,
                        "error": {"code": types.METHOD_NOT_FOUND, "message": "Method not found"},
                    }
                )(scope, receive, send)
        buffered = iter(messages)

        async def bounded_receive():
            try:
                return next(buffered)
            except StopIteration:
                return await receive()

        response_started = False

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except (ValueError, ValidationError, KeyError, TypeError) as error:
            if response_started:
                raise
            await JSONResponse({"error": str(error)[:1000]}, 400)(scope, receive, send)


def create_app(config: ServerConfig):
    service = Service(config)
    management_auth = ManagementAuth(config)
    management = Management(service)
    sources = Sources(management)
    service.sources = sources
    mcp = Server(
        "webcodex",
        version=__version__,
        instructions=(
            "WebCodex supplies host coding, browser, and desktop tools; reasoning and subagents stay in "
            "the host client. Prefer dedicated connectors/APIs first. For web UI work, prefer native "
            "browser tools in background tabs, or use Playwright browser-tools (plugins_list -> "
            "plugin_tools(plugin='browser-tools', server='browser') -> plugin_call). Use computer_* only "
            "for native apps, OS dialogs, or steps browser tools cannot do, and never to bypass permissions.\n"
            "Projects & tasks: Use projects_list -> work_on_project for existing projects, or "
            "project_roots_list -> project_create/project_register -> task_create/work_on_project for new ones. "
            "work_on_project already returns initial Git status and root AGENTS.md; read task AGENTS.md "
            "and matching skills before editing. Use task_takeover for cross-conversation handoff and "
            "preserve existing job_id/process_id instead of restarting commands. Use view_images before "
            "view_image, run tests, call task_review before closeout, and save concise summary/plan via "
            "task_update in the user's language (short Markdown bullets and backtick paths).\n"
            "Waiting & quota rule (mandatory): Call task_present once per user-initiated turn that starts "
            "or resumes sustained work. Long calls return job_id or process_id; use job_get/read_process "
            "with long wait_seconds (20–30s) and keep calling job_get/read_process in the same turn until "
            "the work finishes. Do independent work while waiting and give brief updates only on state "
            "changes or after ~30s. NEVER end the turn, yield, or ask the user to send a 'continue' message "
            "just because only waiting remains—even when the task card is connected. Check latest_execution "
            "outcome and exit_code (job status succeeded only means the RPC returned).\n"
            "Task card & continuation: Automatic messages default off; task_wait monitors existing jobs "
            "without enabling automatic delivery. On an automatic continuation wake, call task_resume first "
            "and stop if already_consumed=true. Do not auto-restart conversations on card inactivity; ask "
            "ordinary questions in chat (request_user_input is CLI-only).\n"
            "Prohibited workers & host errors: Every conversation is prohibited from starting Codex or "
            "Work agents/workers via WebCodex (exec_command, write_stdin, scripts, plugins, MCP providers, "
            "SDKs, or HTTP APIs). If the host blocks developer MCP execution, stop retrying and report "
            "host execution blocked with Runner status unknown; only projects_list/project_roots_list "
            "establishes Runner status."
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
        monitor = ConnectionMonitor(service.store, service.connections)
        monitor.start()
        try:
            async with manager.run():
                yield
        finally:
            monitor.close()
            try:
                await sources.close()
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
                if body.get("supports_long_exec_yield") is not True:
                    for job in jobs:
                        if job["tool"] == "exec_command" and "yield_time_ms" in job["arguments"]:
                            # Dispatch a compatible copy; retries still use the saved request.
                            args = job["arguments"]
                            args["yield_time_ms"] = min(args["yield_time_ms"], 2000)
                return JSONResponse({"jobs": jobs})
            await service.wait_runner_event(
                request.state.owner, min(0.5, max(0, deadline - time.monotonic()))
            )

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
            *management_auth.routes(),
            *management_routes(management, sources),
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
    app.state.management_auth = management_auth
    app.state.management_sources = sources
    app.add_middleware(Security, config=config, management_auth=management_auth)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    app.add_middleware(RequestDiagnostics, diagnostics=service.diagnostics)
    return app
