"""Authenticated task views and status updates for the existing task records."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .store import INTERNAL_OBSERVATION_PREFIXES

PROCESS_FRESH_SECONDS = 90


def requested_status(body: dict) -> str:
    if (
        not isinstance(body, dict)
        or set(body) != {"status"}
        or body["status"] not in ("active", "completed")
    ):
        raise ValueError("Expected active or completed task status")
    return body["status"]


def execution_freshness(task: dict, *, now: float | None = None) -> dict:
    """Project current work for the dashboard without turning history into a blocker."""
    now = time.time() if now is None else now
    task = {
        **task,
        "created_at": task.get("created_at", task.get("created")),
        "started_at": task.get("started_at"),
    }

    def process_view(process):
        unconfirmed = process.get("status") == "running" and (
            not task.get("runner_online")
            or now - (process.get("observed_at") or 0) > PROCESS_FRESH_SECONDS
        )
        return {
            **process,
            "display_status": "unconfirmed" if unconfirmed else process.get("status"),
        }

    task["processes"] = [process_view(process) for process in task.get("processes", [])]
    if "outputs" in task:
        task["outputs"] = [process_view(process) for process in task["outputs"]]
    liveness = {**task.get("liveness", {})}
    work = {**liveness.get("work", {})}
    fresh = sum(process["display_status"] == "running" for process in task["processes"])
    observed = max(
        work.get("running_count", 0),
        sum(process.get("status") == "running" for process in task["processes"]),
    )
    work.update(confirmed_running_count=fresh, unconfirmed_count=max(0, observed - fresh))
    task["liveness"] = {**liveness, "work": work}
    if task.get("status") == "completed":
        task["state"] = "completed"
    elif task.get("state") in {"project_changed", "waiting_operator"}:
        pass
    elif fresh or any(job.get("status") in {"queued", "running"} for job in task.get("jobs", [])):
        task["state"] = "observing" if task.get("runner_online") else "waiting_runner"
    elif task.get("state") == "pending_approval" or any(
        job.get("status") == "pending_approval" for job in task.get("jobs", [])
    ):
        task["state"] = "pending_approval"
    elif work.get("pending_count") or task.get("jobs"):
        task["state"] = "observing" if task.get("runner_online") else "waiting_runner"
    elif work["unconfirmed_count"]:
        task["state"] = "needs_confirmation"
    else:
        task["state"] = "ready"
    return task


class Management:
    def __init__(self, service):
        self.service = service
        self.observed: dict[tuple[str, str], float] = {}
        self.inflight: dict[tuple[str, str], str] = {}
        self.lock = asyncio.Lock()

    def owner(self, request) -> str:
        owner = request.state.owner
        client = self.service.config.clients.get(owner)
        if client is None or "read" not in client.permissions:
            raise ValueError("Read permission is required")
        return owner

    def task(self, owner: str, task_id: str) -> dict:
        task = self.service.store.task(task_id, owner)
        self.service.check_project(owner, task["runner"] + "/" + task["project"], task["kind"])
        return task

    def describe(self, task: dict) -> dict:
        progress = self.service.task_progress(task, include_output=False)
        processes = progress["process_observations"]
        if progress["liveness"]["work"]["running_count"] > sum(
            p["status"] == "running" for p in processes
        ):
            older = self.service.store.rows(
                "WITH latest AS (SELECT id AS job_id,tool,updated AS observed_at,"
                "process_id,process_status AS status,process_exit_code AS exit_code,"
                "min(created) OVER (PARTITION BY process_id) AS started_at,"
                "row_number() OVER (PARTITION BY process_id ORDER BY updated DESC,id DESC) AS rank "
                "FROM jobs WHERE task_id=? AND status='succeeded' AND process_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM agent_observations a "
                "WHERE a.task_id=jobs.task_id AND a.agent_id=jobs.process_id) "
                "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files')) "
                "SELECT job_id,tool,observed_at,started_at,process_id,status,exit_code FROM latest "
                "WHERE rank=1 AND status='running'",
                (task["id"],),
            )
            visible = {p["process_id"] for p in processes}
            processes.extend(p for p in older if p["process_id"] not in visible)
        processes.sort(key=lambda p: (p["status"] != "running", -p["started_at"], p["process_id"]))
        # The standalone page covers the whole task, not one ChatGPT window.
        activity = self.service.store.one(
            "SELECT tool,started_at,finished_at,outcome FROM task_tool_activity WHERE task_id=? "
            "ORDER BY coalesce(finished_at,started_at) DESC LIMIT 1",
            (task["id"],),
        )
        external = " AND ".join("request_id NOT LIKE ?" for _ in INTERNAL_OBSERVATION_PREFIXES)
        pending = self.service.store.rows(
            "SELECT id AS job_id,tool,status,updated,count(*) OVER () AS pending_count "
            "FROM jobs INDEXED BY pending_jobs WHERE owner=? AND task_id=? "
            "AND status IN ('queued','pending_approval','running') "
            f"AND (request_id IS NULL OR ({external})) ORDER BY created LIMIT 100",
            (
                task["owner"],
                task["id"],
                *(prefix + "%" for prefix in INTERNAL_OBSERVATION_PREFIXES),
            ),
        )
        work = progress["liveness"]["work"]
        had_pending = bool(work["pending_count"])
        work["pending_count"] = pending[0]["pending_count"] if pending else 0
        # Looking at a task must not make an idle task appear to be doing new work.
        if (
            progress["state"] in {"observing", "waiting_runner"}
            and had_pending
            and not pending
            and not work["running_count"]
        ):
            progress["state"] = (
                "waiting_operator" if progress["unanswered_operator_questions"] else "ready"
            )
        started = self.service.store.one(
            "SELECT created AS started_at FROM jobs WHERE task_id=? "
            f"AND (request_id IS NULL OR ({external})) ORDER BY created LIMIT 1",
            (task["id"], *(prefix + "%" for prefix in INTERNAL_OBSERVATION_PREFIXES)),
        )
        job_activity = self.service.store.one(
            "SELECT tool,created AS started_at,updated AS finished_at,status AS outcome "
            f"FROM jobs WHERE task_id=? AND (request_id IS NULL OR ({external})) "
            "ORDER BY updated DESC,id DESC LIMIT 1",
            (task["id"], *(prefix + "%" for prefix in INTERNAL_OBSERVATION_PREFIXES)),
        )
        if job_activity and (
            not activity
            or job_activity["finished_at"] > (activity["finished_at"] or activity["started_at"])
        ):
            activity = job_activity
        progress["liveness"]["conversation"] = activity
        return execution_freshness(
            {
                **{
                    k: task[k]
                    for k in ("id", "title", "runner", "project", "status", "summary", "updated")
                },
                "runner_online": progress["runner_online"],
                "created_at": task["created"],
                "started_at": started["started_at"] if started else None,
                "state": progress["state"],
                "processes": processes,
                "jobs": [
                    {k: j.get(k) for k in ("job_id", "tool", "status", "updated")} for j in pending
                ],
                "issues": progress["recent_issues"],
                "liveness": progress["liveness"],
                "stale": not progress["runner_online"],
            }
        )

    async def listing(self, owner: str, offset: int = 0, limit: int = 60) -> dict:
        listed = await self.service.call(owner, "task_list", {"offset": offset, "limit": limit})
        return {
            "tasks": [self.describe(self.task(owner, row["id"])) for row in listed["tasks"]],
            "next_offset": listed["next_offset"],
        }

    async def state(self, request):
        owner = self.owner(request)
        offset = int(request.query_params.get("offset", 0))
        if offset < 0:
            raise ValueError("Invalid offset")
        return JSONResponse(
            await self.state_data(owner, offset), headers={"Cache-Control": "no-store"}
        )

    def runners(self, owner: str) -> list[dict]:
        allowed = self.service.allowed_projects(owner)
        roots = self.service.config.clients[owner].project_roots
        runner_ids = {p.split("/", 1)[0] for p in [*allowed, *roots]}
        return [
            {"id": r["id"], "last_seen": r["seen"], "online": time.time() - r["seen"] < 15}
            for r in self.service.store.rows("SELECT id,seen FROM runners ORDER BY id")
            if r["id"] in runner_ids
        ]

    async def state_data(self, owner: str, offset: int = 0) -> dict:
        result = await self.listing(owner, offset)
        result["runners"] = self.runners(owner)
        result["observed_at"] = time.time()
        return result

    async def refresh(self, request):
        owner = self.owner(request)
        body = await request.json()
        return JSONResponse(
            await self.refresh_data(owner, body), headers={"Cache-Control": "no-store"}
        )

    async def refresh_data(self, owner: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValueError("Expected a refresh object")
        task_id = body.get("task_id")
        task_ids = body.get("task_ids")
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise ValueError("Invalid task ID")
        if task_ids is not None and (
            not isinstance(task_ids, list)
            or len(task_ids) > 100
            or any(not isinstance(value, str) or not value for value in task_ids)
        ):
            raise ValueError("Expected at most 100 task IDs")
        if task_id is not None and task_ids is not None:
            raise ValueError("Use task_id or task_ids")
        if task_ids is None and task_id is not None:
            task_ids = [task_id]
        tasks = (
            [self.describe(self.task(owner, value)) for value in dict.fromkeys(task_ids)]
            if task_ids is not None
            else (await self.listing(owner))["tasks"]
        )
        candidates = sorted(
            (
                (task, p)
                for task in tasks
                if task["runner_online"]
                for p in task["processes"]
                if p["status"] == "running"
            ),
            key=lambda pair: self.observed.get((owner, pair[1]["process_id"]), 0),
        )
        updates = []
        async with self.lock:
            # Limits are shared across tabs. Observe the same pending job on retries.
            for task, process in candidates:
                key = (owner, process["process_id"])
                if time.monotonic() - self.observed.get(key, 0) < 10:
                    continue
                if len(updates) >= 4:
                    break
                self.observed[key] = time.monotonic()
                try:
                    if key in self.inflight:
                        result = await self.service.call(
                            owner,
                            "job_get",
                            {
                                "job_id": self.inflight[key],
                                "wait_seconds": 0,
                            },
                        )
                    else:
                        prior = self.service.store.job(process["job_id"], owner).get("result") or {}
                        result = await self.service.call(
                            owner,
                            "read_process",
                            {
                                "task_id": task["id"],
                                "process_id": process["process_id"],
                                "cursor": max(0, int(prior.get("next_cursor", 0))),
                                "max_bytes": 16384,
                                "wait_seconds": 0,
                                "request_id": "manage-observe:" + process["job_id"],
                            },
                        )
                    if result.get("status") in {"queued", "running", "pending_approval"}:
                        self.inflight[key] = result["job_id"]
                    else:
                        self.inflight.pop(key, None)
                    updates.append(
                        {
                            "process_id": process["process_id"],
                            "job_id": result.get("job_id"),
                            "status": result.get("status"),
                        }
                    )
                except ValueError as error:
                    updates.append({"process_id": process["process_id"], "error": str(error)[:500]})
            if len(self.observed) > 2000:
                cutoff = time.monotonic() - 600
                self.observed = {
                    k: v for k, v in self.observed.items() if v >= cutoff or k in self.inflight
                }
        return {"updates": updates, "observed_at": time.time()}

    async def detail(self, request):
        owner = self.owner(request)
        return JSONResponse(
            self.detail_data(owner, request.path_params["task_id"]),
            headers={"Cache-Control": "no-store"},
        )

    async def update_status(self, owner: str, task_id: str, status: str) -> dict:
        task = self.task(owner, task_id)
        if task["kind"] != "coding":
            raise ValueError("專案管理紀錄需保持開啟")
        # The local task_update path does not yield between reading and saving these notes.
        updated = await self.service.traced_call(
            owner,
            "task_update",
            {
                "task_id": task_id,
                "status": status,
                "summary": task["summary"],
                "plan": task["plan"],
            },
            track_activity=True,
        )
        return {key: updated[key] for key in ("id", "status", "updated")}

    async def set_status(self, request):
        owner = self.owner(request)
        if "write" not in self.service.config.clients[owner].permissions:
            return JSONResponse({"error": "沒有更新任務的權限"}, 403)
        status = requested_status(await request.json())
        task_id = request.path_params["task_id"]
        try:
            self.task(owner, task_id)
        except ValueError:
            return JSONResponse({"error": "任務不存在或無權存取"}, 404)
        return JSONResponse(
            await self.update_status(owner, task_id, status),
            headers={"Cache-Control": "no-store"},
        )

    def detail_data(self, owner: str, task_id: str) -> dict:
        task = self.task(owner, task_id)
        result = self.describe(task)
        result["plan"] = task["plan"]
        result["outputs"] = []
        for process in result["processes"][:20]:
            # A later observation may be empty. Show the most recent non-empty output too.
            jobs = self.service.store.rows(
                "SELECT result,updated FROM jobs WHERE task_id=? AND process_id=? "
                "AND status='succeeded' AND process_id IS NOT NULL "
                "AND tool IN ('exec_command','read_process','cancel_process','skill_run','search_files') "
                "AND CASE WHEN json_valid(result) THEN "
                "json_type(result,'$.output')='text' AND json_extract(result,'$.output')!='' "
                "ELSE 0 END ORDER BY updated DESC,id DESC LIMIT 12",
                (task["id"], process["process_id"]),
            )
            chunks = []
            end = 0
            for job in reversed(jobs):
                payload = json.loads(job["result"]) if job["result"] else {}
                output = payload.get("output")
                if output:
                    start = int(payload.get("cursor", 0))
                    encoded = output.encode("utf-8")
                    if start + len(encoded) > end:
                        chunks.append(
                            encoded[max(0, end - start) :].decode("utf-8", errors="replace")
                        )
                        end = start + len(encoded)
            result["outputs"].append(
                {
                    **process,
                    "output": "".join(chunks)[-32768:],
                }
            )
        result["errors"] = self.service.store.rows(
            "SELECT id AS job_id,tool,status,result,updated FROM jobs WHERE task_id=? "
            "AND status IN ('failed','unknown') ORDER BY updated DESC LIMIT 10",
            (task["id"],),
        )
        for issue in result["errors"]:
            issue["result"] = json.loads(issue["result"]) if issue["result"] else None
        result["observed_at"] = time.time()
        return execution_freshness(result)


def management_routes(management: Management, sources) -> list[Route]:
    from .management_sources import source_routes

    html = (Path(__file__).parent / "management.html").read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()

    async def page(request):
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; img-src data:; connect-src 'self'; style-src 'unsafe-inline'; "
                    f"script-src 'sha256-{script_hash}'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                ),
            },
        )

    return [
        Route("/manage", page, methods=["GET"]),
        Route("/api/manage/state", management.state, methods=["GET"]),
        Route("/api/manage/refresh", management.refresh, methods=["POST"]),
        Route("/api/manage/tasks/{task_id}", management.detail, methods=["GET"]),
        Route("/api/manage/tasks/{task_id}/status", management.set_status, methods=["POST"]),
        *source_routes(sources),
    ]
