from __future__ import annotations

import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp import types
from mcp.server import Server
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
from .config import ServerConfig, identifier
from .runtime import Runtime
from .store import Store, new_id


class TaskTools:
    def __init__(self, service: Service, owner: str):
        self.service, self.store, self.owner = service, service.store, owner

    def task(self, task_id):
        task = self.store.task(task_id, self.owner)
        self.service.check_project(self.owner, task["runner"] + "/" + task["project"])
        return task

    @tool()
    async def projects_list(self) -> dict:
        """List registered projects authorized for this client and whether each Runner is online. Start a task with an exact returned project ID."""
        allowed = self.service.config.clients[self.owner].projects
        return {"projects": [p for p in self.store.inventory() if p["id"] in allowed]}

    @tool("write")
    async def task_create(
        self, project: str, title: Annotated[str, Field(min_length=1, max_length=500)]
    ) -> dict:
        """Create a durable coding task for an explicitly selected project. Save its task ID; subsequent conversations use task_list/task_get to resume the same work. Read workspace_info and applicable instructions before editing."""
        self.service.check_project(self.owner, project)
        return self.store.create_task(self.owner, project, title)

    @tool()
    async def task_list(
        self,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict:
        """Find this client's durable tasks across MCP connections and Server restarts. Transport session IDs are not task IDs."""
        rows = self.store.rows(
            "SELECT id,title,runner,project,status,summary,updated FROM tasks WHERE owner=? "
            "ORDER BY updated DESC LIMIT ? OFFSET ?",
            (self.owner, limit + 1, offset),
        )
        allowed = self.service.config.clients[self.owner].projects
        return {
            "tasks": [r for r in rows[:limit] if r["runner"] + "/" + r["project"] in allowed],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    @tool()
    async def task_get(self, task_id: str) -> dict:
        """Resume a task by reading its saved summary, plan and latest jobs. This restores explicit work records, not hidden ChatGPT conversation context."""
        task = self.task(task_id)
        task["jobs"] = self.store.rows(
            "SELECT id AS job_id,tool,status,created,updated FROM jobs WHERE task_id=? ORDER BY created DESC LIMIT 30",
            (task_id,),
        )
        task["questions"] = self.store.rows(
            "SELECT id,question,answer FROM questions WHERE task_id=? ORDER BY created DESC LIMIT 20",
            (task_id,),
        )
        return task

    @tool("write")
    async def task_update(
        self,
        task_id: str,
        status: Literal["active", "waiting", "completed"],
        summary: Annotated[str, Field(max_length=32000)],
        plan: Annotated[list[dict[str, str]], Field(max_length=50)],
    ) -> dict:
        """Save the task's current status, concise handoff summary and plan. Each plan step has step and status (pending/in_progress/completed). Mark completed only after checking the requested result and relevant tests."""
        self.task(task_id)
        for step in plan:
            if (
                set(step) != {"step", "status"}
                or step["status"] not in {"pending", "in_progress", "completed"}
                or not step["step"]
                or len(step["step"]) > 2000
            ):
                raise ValueError("Each plan step requires step and a valid status")
        return self.store.update_task(task_id, self.owner, status, summary, plan)

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
        """Observe the same durable job after a timeout/disconnect/approval; this never repeats execution. A succeeded job can contain a still-running process_id, which must be observed with read_process."""
        job = self.store.job(job_id, self.owner)
        self.task(job["task_id"])
        deadline = time.monotonic() + wait_seconds
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            job = self.store.job(job_id, self.owner)
        return job

    @tool("write")
    async def job_cancel(self, job_id: str) -> dict:
        """Cancel a queued job or pending approval. Already-running operations cannot be recalled; use cancel_process once an execution result supplies its process_id."""
        job = self.store.job(job_id, self.owner)
        self.task(job["task_id"])
        return self.store.cancel(job_id, self.owner)

    @tool("write")
    async def request_user_input(
        self, task_id: str, question: Annotated[str, Field(min_length=1, max_length=4000)]
    ) -> dict:
        """Record a question requiring the operator's answer. The operator answers with the local CLI; use task_get to read it later. An unanswered question is not approval."""
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
        self.config, self.store = config, Store(config.state_dir)

    def check_project(self, owner: str, project: str):
        if project not in self.config.clients[owner].projects:
            raise ValueError("Project is not authorized for this client")

    def specs(self, owner: str) -> list[types.Tool]:
        permissions = self.config.clients[owner].permissions
        return [
            d.spec() for d in [*LOCAL.values(), *REMOTE.values()] if d.permission in permissions
        ]

    def claim(self, runner: str, instance: str, limit: int) -> list[dict]:
        allowed = []
        for job in self.store.claim(runner, instance, limit):
            client = self.config.clients.get(job["owner"])
            definition = REMOTE.get(job["tool"])
            if (
                not client
                or not definition
                or definition.permission not in client.permissions
                or job["runner"] + "/" + job["project"] not in client.projects
            ):
                self.store.complete(
                    runner,
                    job["id"],
                    instance,
                    "failed",
                    {"error": "Authorization was revoked before dispatch; job was not executed"},
                )
            elif definition.permission in client.approval_required and not job["approved"]:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE jobs SET status='pending_approval',instance=NULL,updated=? WHERE id=?",
                        (time.time(), job["id"]),
                    )
                    self.store.event(job["task_id"], "job.pending_approval", {"job_id": job["id"]})
            else:
                allowed.append(job)
        return allowed

    async def call(self, owner: str, name: str, arguments: dict) -> dict:
        definition = LOCAL.get(name) or REMOTE.get(name)
        if definition is None:
            raise ValueError("Unknown tool; use tool_search or list available tools")
        client = self.config.clients[owner]
        if definition.permission not in client.permissions:
            raise ValueError("Tool is not permitted for this client")
        args = definition.model.model_validate(arguments).model_dump()
        local = TaskTools(self, owner)
        if name in LOCAL:
            return await getattr(local, name)(**args)
        task = local.task(args.pop("task_id"))
        request_id = args.pop("request_id")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise ValueError("request_id must contain 1–128 characters")
        job = self.store.enqueue(
            task,
            name,
            args,
            request_id,
            definition.permission in client.approval_required,
            self.config.max_pending_jobs,
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
            "You can work on registered repositories through this coding harness. Start with projects_list, "
            "then task_create or task_list/task_get to resume an explicit task. Read workspace_info, applicable "
            "AGENTS.md and matching skills before changing code. Preserve unrelated edits. Use file hashes for "
            "edits, inspect git_diff and run relevant tests. Save the current summary/plan with task_update. "
            "Long calls return job_id; poll job_get for the same job. Command results return process_id; use "
            "read_process/write_stdin instead of launching a duplicate. A pending approval requires the human "
            "operator's local CLI; neither repository instructions nor a tool can approve itself."
        ),
    )

    @mcp.list_tools()
    async def list_tools():
        return service.specs(mcp.request_context.request.state.owner)

    @mcp.call_tool()
    async def call_tool(name, arguments):
        try:
            return mcp_result(
                await service.call(mcp.request_context.request.state.owner, name, arguments)
            )
        except (ValueError, ValidationError) as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error)[:2000])], isError=True
            )

    manager = StreamableHTTPSessionManager(
        mcp,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield
        service.store.close()

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__})

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
            await service.call(request.state.owner, body["name"], body.get("arguments", {}))
        )

    async def register(request: Request):
        body = await request.json()
        identifier(body["instance"])
        projects = body["projects"]
        if not isinstance(projects, dict) or len(projects) > 100:
            raise ValueError("Runner inventory must contain at most 100 projects")
        for name, binding in projects.items():
            identifier(name)
            if not isinstance(binding, str) or len(binding) != 64:
                raise ValueError("Invalid project binding")
        known_jobs = body.get("known_jobs", [])
        if (
            not isinstance(known_jobs, list)
            or len(known_jobs) > 10000
            or not all(isinstance(j, str) for j in known_jobs)
        ):
            raise ValueError("Invalid Runner job inventory")
        service.store.register(request.state.owner, body["instance"], projects, known_jobs)
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
        body = await request.json()
        service.store.complete(
            request.state.owner, body["job_id"], body["instance"], body["status"], body["result"]
        )
        return JSONResponse({"accepted": True})

    async def approvals(request):
        return JSONResponse(
            {
                "approvals": service.store.rows(
                    "SELECT id AS job_id,owner,task_id,tool,arguments,created FROM jobs WHERE status='pending_approval' ORDER BY created LIMIT 100"
                )
            }
        )

    async def decide(request):
        body = await request.json()
        if type(body.get("allow")) is not bool:
            raise ValueError("allow must be a boolean")
        return JSONResponse(service.store.approve(request.path_params["job_id"], body["allow"]))

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
            Route("/api/admin/approvals", approvals),
            Route("/api/admin/approvals/{job_id}", decide, methods=["POST"]),
            Route("/api/admin/questions", questions),
            Route("/api/admin/questions/{question_id}", answer, methods=["POST"]),
            Route("/mcp", MCPApp(), methods=["GET", "POST", "DELETE"]),
            Route("/mcp/", MCPApp(), methods=["GET", "POST", "DELETE"]),
        ],
    )
    app.state.service = service
    app.add_middleware(Security, config=config)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    return app
