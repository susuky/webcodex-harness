"""Authenticated, read-only views of the existing task and process records."""

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
        return {
            **{
                k: task[k]
                for k in ("id", "title", "runner", "project", "status", "summary", "updated")
            },
            "runner_online": progress["runner_online"],
            "state": progress["state"],
            "processes": processes,
            "jobs": [
                {k: j.get(k) for k in ("job_id", "tool", "status", "updated")}
                for j in progress["pending_jobs"]
            ],
            "issues": progress["recent_issues"],
            "liveness": progress["liveness"],
            "stale": not progress["runner_online"],
        }

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
        result = await self.listing(owner, offset)
        allowed = self.service.allowed_projects(owner)
        roots = self.service.config.clients[owner].project_roots
        runner_ids = {p.split("/", 1)[0] for p in [*allowed, *roots]}
        result["runners"] = [
            {"id": r["id"], "last_seen": r["seen"], "online": time.time() - r["seen"] < 15}
            for r in self.service.store.rows("SELECT id,seen FROM runners ORDER BY id")
            if r["id"] in runner_ids
        ]
        result["observed_at"] = time.time()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def refresh(self, request):
        owner = self.owner(request)
        body = await request.json()
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
        return JSONResponse(
            {"updates": updates, "observed_at": time.time()}, headers={"Cache-Control": "no-store"}
        )

    async def detail(self, request):
        owner = self.owner(request)
        task = self.task(owner, request.path_params["task_id"])
        result = self.describe(task)
        result["outputs"] = []
        for process in result["processes"][:20]:
            # A later observation may be empty. Show the most recent non-empty output too.
            jobs = self.service.store.rows(
                "SELECT result,updated FROM jobs WHERE task_id=? AND process_id=? "
                "AND status='succeeded' AND CASE WHEN json_valid(result) THEN "
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
        return JSONResponse(result, headers={"Cache-Control": "no-store"})


def management_routes(service) -> list[Route]:
    management = Management(service)
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
                    "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                    f"script-src 'sha256-{script_hash}'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                ),
            },
        )

    return [
        Route("/manage", page, methods=["GET"]),
        Route("/api/manage/state", management.state, methods=["GET"]),
        Route("/api/manage/refresh", management.refresh, methods=["POST"]),
        Route("/api/manage/tasks/{task_id}", management.detail, methods=["GET"]),
    ]
