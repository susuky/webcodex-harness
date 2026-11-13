"""Client-scoped task views of explicitly configured WebCodex Servers."""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import identifier
from .management import PROCESS_FRESH_SECONDS, execution_freshness, requested_status
from .store import INTERNAL_OBSERVATION_PREFIXES

PROCESS_TOOLS = "('exec_command','read_process','cancel_process','skill_run','search_files')"


class SourceUnavailable(Exception):
    pass


class TaskUnavailable(Exception):
    pass


def priority(task: dict) -> int:
    if task.get("status") == "completed":
        return 3
    if "_priority" in task:
        return task["_priority"]
    work = (task.get("liveness") or {}).get("work") or {}
    jobs = task.get("jobs", [])
    active_pending = any(job.get("status") in {"queued", "running"} for job in jobs)
    if not jobs:
        active_pending = bool(work.get("pending_count")) and task.get("state") != "pending_approval"
    if (
        work.get("confirmed_running_count", work.get("running_count"))
        or active_pending
        or any(
            p.get("display_status", p.get("status")) == "running" for p in task.get("processes", [])
        )
    ):
        return 0
    if any(job.get("status") == "pending_approval" for job in jobs):
        return 1
    return (
        1 if task.get("state") in {"waiting_operator", "pending_approval", "project_changed"} else 2
    )


class Sources:
    def __init__(self, management):
        self.management = management
        self.service = management.service
        self.catalogs: dict[tuple[str, str], tuple[float, dict]] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.creation_times: dict[tuple[str, str, str], float | None] = {}
        self.creation_lock = asyncio.Lock()
        self.clients: dict[tuple[str, str, str, str], httpx.AsyncClient] = {}
        # Archiving is a client-scoped view preference, including for remote tasks.
        with self.service.store.db:
            self.service.store.db.execute(
                "CREATE TABLE IF NOT EXISTS management_archives ("
                "owner TEXT NOT NULL,source_id TEXT NOT NULL,task_id TEXT NOT NULL,"
                "archived_at REAL NOT NULL,PRIMARY KEY(owner,source_id,task_id))"
            )

    def source(self, owner: str, source_id: str):
        identifier(source_id)
        if source_id == "local":
            return None
        source = self.service.config.clients[owner].management_sources.get(source_id)
        if source is None:
            raise ValueError("Management source is not available to this client")
        return source

    def label(self, owner: str, source_id: str) -> str:
        source = self.source(owner, source_id)
        return (source.label or source_id) if source else "本機"

    def environment_label(self, owner: str, source_id: str, runner: str) -> str:
        source = self.source(owner, source_id)
        return (source.runner_labels.get(runner) if source else None) or self.label(
            owner, source_id
        )

    def archived_ids(self, owner: str, source_id: str) -> set[str]:
        return {
            row["task_id"]
            for row in self.service.store.rows(
                "SELECT task_id FROM management_archives WHERE owner=? AND source_id=?",
                (owner, source_id),
            )
        }

    def tag(self, owner: str, source_id: str, row: dict) -> dict:
        result = {**row, "source_id": source_id, "source_label": self.label(owner, source_id)}
        if "project" in row:
            result["environment_label"] = self.environment_label(owner, source_id, row["runner"])
            if "archived" not in result:
                result["archived"] = row["id"] in self.archived_ids(owner, source_id)
        if "project" in row and row.get("created_at") is None:
            created = self.creation_times.get((owner, source_id, row["id"]))
            if created is not None:
                result["created_at"] = created
        return result

    @staticmethod
    def response(data: dict, status_code: int = 200):
        return JSONResponse(data, status_code, headers={"Cache-Control": "no-store"})

    async def listing(self, request):
        owner = self.management.owner(request)
        sources = self.service.config.clients[owner].management_sources
        local = (
            not sources
            or self.service.allowed_projects(owner)
            or self.service.config.clients[owner].project_roots
        )
        return self.response(
            {
                "sources": [
                    {
                        "id": source_id,
                        "label": self.label(owner, source_id),
                        "runner_labels": sources[source_id].runner_labels
                        if source_id != "local"
                        else {},
                        "can_update_tasks": "write"
                        in self.service.config.clients[owner].permissions,
                    }
                    for source_id in (["local"] if local else []) + list(sources)
                ]
            }
        )

    def local_catalog(self, owner: str) -> list[dict]:
        allowed = sorted(self.service.allowed_projects(owner))
        roots = self.service.config.clients[owner].project_roots
        project_slots = ",".join("?" for _ in allowed) or "NULL"
        root_slots = ",".join("?" for _ in roots) or "NULL"
        external = " AND ".join("request_id NOT LIKE ?" for _ in INTERNAL_OBSERVATION_PREFIXES)
        activity = (
            "SELECT max(coalesce(finished_at,started_at)) FROM task_tool_activity "
            "WHERE task_id=tasks.id"
        )
        tasks = self.service.store.rows(
            "SELECT id,title,runner,project,kind,binding,status,summary,updated,created AS created_at,"
            # Seek from the newest job and stop at the tracked activity, without reading old output.
            "coalesce((SELECT updated FROM jobs INDEXED BY task_jobs WHERE task_id=tasks.id "
            f"AND updated>coalesce(({activity}),0) AND (request_id IS NULL OR ({external})) "
            f"ORDER BY updated DESC LIMIT 1),({activity})) AS _activity FROM tasks WHERE owner=? "
            f"AND ((kind='coding' AND runner||'/'||project IN ({project_slots})) "
            f"OR (kind='project' AND runner||'/'||project IN ({root_slots})))",
            (*(prefix + "%" for prefix in INTERNAL_OBSERVATION_PREFIXES), owner, *allowed, *roots),
        )
        if not tasks:
            return tasks
        by_id = {t["id"]: t for t in tasks}
        slots = ",".join("?" for _ in tasks)
        live, attention, unconfirmed = set(), set(), set()
        online = {runner["id"] for runner in self.management.runners(owner) if runner["online"]}
        fresh_after = time.time() - PROCESS_FRESH_SECONDS
        # Read the indexed process projection; command output can be many megabytes.
        observations = self.service.store.rows(
            "WITH latest AS (SELECT task_id,process_status AS status,updated,"
            "row_number() OVER (PARTITION BY task_id,process_id ORDER BY updated DESC,id DESC) AS rank "
            f"FROM jobs WHERE task_id IN ({slots}) AND status='succeeded' "
            "AND NOT EXISTS (SELECT 1 FROM agent_observations a "
            "WHERE a.task_id=jobs.task_id AND a.agent_id=jobs.process_id) "
            f"AND process_id IS NOT NULL AND tool IN {PROCESS_TOOLS}) "
            "SELECT task_id,status,updated FROM latest WHERE rank=1 UNION ALL "
            f"SELECT task_id,status,updated FROM agent_observations WHERE task_id IN ({slots})",
            (*by_id, *by_id),
        )
        for row in observations:
            if row["status"] == "running":
                if row["updated"] >= fresh_after and by_id[row["task_id"]]["runner"] in online:
                    live.add(row["task_id"])
                else:
                    unconfirmed.add(row["task_id"])
        for row in self.service.store.rows(
            "SELECT task_id,status,max(updated) AS updated FROM jobs INDEXED BY pending_jobs "
            f"WHERE owner=? AND task_id IN ({slots}) AND status IN ('queued','pending_approval','running') "
            f"AND (request_id IS NULL OR ({external})) "
            "GROUP BY task_id,status",
            (owner, *by_id, *(prefix + "%" for prefix in INTERNAL_OBSERVATION_PREFIXES)),
        ):
            if row["status"] in {"queued", "running"}:
                live.add(row["task_id"])
            else:
                attention.add(row["task_id"])
        attention.update(
            row["task_id"]
            for row in self.service.store.rows(
                f"SELECT DISTINCT task_id FROM questions WHERE task_id IN ({slots}) AND answer IS NULL",
                tuple(by_id),
            )
        )
        bindings = {
            (kind, entry["id"]): entry["binding"]
            for kind, roots in (("coding", False), ("project", True))
            for entry in self.service.store.inventory(roots=roots)
        }
        for task in tasks:
            if (
                bindings.get((task["kind"], task["runner"] + "/" + task["project"]))
                != task["binding"]
            ):
                attention.add(task["id"])
            task["_unconfirmed"] = task["id"] in unconfirmed
            task["_attention"] = task["id"] in attention
            task["_priority"] = (
                3
                if task["status"] == "completed"
                else 0
                if task["id"] in live
                else 1
                if task["id"] in attention
                else 2
            )
        return tasks

    async def remote_request(self, client, method: str, path: str, **kwargs) -> dict:
        try:
            response = await client.request(method, "/api/manage/" + path, **kwargs)
            if response.status_code in {401, 403}:
                raise SourceUnavailable("來源驗證失敗或權限已撤銷")
            if method == "GET" and path.startswith("tasks/") and response.status_code in {400, 404}:
                raise TaskUnavailable("任務不存在或無權存取")
            if not response.is_success:
                raise SourceUnavailable(f"來源回應錯誤（HTTP {response.status_code}）")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except httpx.ConnectTimeout:
            raise SourceUnavailable("連線至來源逾時") from None
        except httpx.ReadTimeout:
            raise SourceUnavailable("來源回應逾時") from None
        except httpx.TimeoutException:
            raise SourceUnavailable("來源通訊逾時") from None
        except httpx.HTTPError:
            raise SourceUnavailable("無法連線至來源") from None
        except ValueError:
            raise SourceUnavailable("來源回傳了無效資料") from None

    @asynccontextmanager
    async def remote_client(self, owner: str, source_id: str):
        source = self.source(owner, source_id)
        key = (owner, source_id, source.url, source.token)
        client = self.clients.get(key)
        if client is None:
            client = self.clients[key] = httpx.AsyncClient(
                base_url=source.url,
                headers={"Authorization": "Bearer " + source.token},
                timeout=15,
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=5, keepalive_expiry=30
                ),
                follow_redirects=False,
                trust_env=False,
            )
        yield client

    async def close(self):
        clients, self.clients = self.clients, {}
        await asyncio.gather(*(client.aclose() for client in clients.values()))

    async def remote_creation_time(self, owner: str, source_id: str, task_id: str) -> float | None:
        key = (owner, source_id, task_id)
        async with self.creation_lock:
            if key in self.creation_times:
                return self.creation_times[key]
            try:
                async with self.remote_client(owner, source_id) as client:
                    response = await client.post(
                        "/api/call",
                        timeout=3,
                        json={
                            "name": "task_events",
                            "arguments": {"task_id": task_id, "after": 0, "limit": 1},
                        },
                    )
                if response.status_code in {400, 404}:
                    raise TaskUnavailable("任務不存在或無權存取")
                if not response.is_success:
                    raise SourceUnavailable(f"讀取任務日期失敗（HTTP {response.status_code}）")
                payload = response.json()
                events = payload.get("events") if isinstance(payload, dict) else None
                if not isinstance(events, list) or len(events) > 1:
                    raise SourceUnavailable("來源回傳了無效的任務日期")
                created = None
                if events:
                    event = events[0]
                    if not isinstance(event, dict) or event.get("task_id") != task_id:
                        raise SourceUnavailable("來源回傳了不同的任務事件")
                    if event.get("kind") == "task.created":
                        created = event.get("created")
                        if type(created) not in {int, float} or not math.isfinite(created):
                            raise SourceUnavailable("來源回傳了無效的任務日期")
            except httpx.TimeoutException:
                raise SourceUnavailable("讀取任務日期逾時") from None
            except httpx.HTTPError:
                raise SourceUnavailable("讀取任務日期時無法連線至來源") from None
            except ValueError:
                raise SourceUnavailable("來源回傳了無效的任務日期") from None
            if len(self.creation_times) >= 4096:
                self.creation_times.pop(next(iter(self.creation_times)))
            self.creation_times[key] = created
            return created

    @staticmethod
    def validate_remote_task(task):
        if not isinstance(task, dict) or any(
            not isinstance(task.get(field), str)
            for field in ("id", "title", "runner", "project", "status")
        ):
            raise SourceUnavailable("來源回傳了無效的任務內容")
        for field in ("updated", "created_at", "started_at"):
            value = task.get(field)
            if value is None and field != "updated":
                continue
            if type(value) not in {int, float} or not math.isfinite(value):
                raise SourceUnavailable("來源回傳了無效的任務時間")
        if "runner_online" in task and type(task["runner_online"]) is not bool:
            raise SourceUnavailable("來源回傳了無效的連線狀態")
        for field in ("processes", "outputs", "jobs", "issues", "errors"):
            rows = task.get(field, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise SourceUnavailable("來源回傳了無效的任務內容")
            if field in {"processes", "outputs"}:
                for row in rows:
                    if not isinstance(row.get("status"), str):
                        raise SourceUnavailable("來源回傳了無效的程序狀態")
                    observed = row.get("observed_at")
                    if observed is not None and (
                        type(observed) not in {int, float} or not math.isfinite(observed)
                    ):
                        raise SourceUnavailable("來源回傳了無效的程序觀察時間")
        liveness = task.get("liveness", {})
        if not isinstance(liveness, dict) or not isinstance(liveness.get("work", {}), dict):
            raise SourceUnavailable("來源回傳了無效的工作狀態")
        work = liveness.get("work", {})
        if any(
            field in work and (type(work[field]) is not int or work[field] < 0)
            for field in ("running_count", "pending_count")
        ):
            raise SourceUnavailable("來源回傳了無效的工作狀態")
        activity = liveness.get("conversation")
        if activity is not None:
            if not isinstance(activity, dict) or any(
                value is not None and (type(value) not in {int, float} or not math.isfinite(value))
                for value in (activity.get("started_at"), activity.get("finished_at"))
            ):
                raise SourceUnavailable("來源回傳了無效的任務時間")

    async def remote_catalog(self, owner: str, source_id: str) -> dict:
        key = (owner, source_id)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.catalogs.get(key)
            if cached and time.monotonic() - cached[0] < 3:
                return cached[1]
            tasks, seen, offset = [], set(), 0
            try:
                async with asyncio.timeout(15), self.remote_client(owner, source_id) as client:
                    # Old Servers expose only chronological pages. Fetch their catalog before filtering.
                    for _ in range(100):
                        page = await self.remote_request(
                            client, "GET", "state", params={"offset": offset}
                        )
                        if (
                            not isinstance(page.get("tasks"), list)
                            or not isinstance(page.get("runners"), list)
                            or any(not isinstance(runner, dict) for runner in page["runners"])
                            or not isinstance(page.get("observed_at"), (int, float))
                            or not math.isfinite(page["observed_at"])
                        ):
                            raise SourceUnavailable("來源回傳了無效的任務清單")
                        for task in page["tasks"]:
                            self.validate_remote_task(task)
                            if task["id"] not in seen:
                                tasks.append(task)
                                seen.add(task["id"])
                        next_offset = page.get("next_offset")
                        if next_offset is None:
                            result = {**page, "tasks": tasks, "next_offset": None}
                            self.catalogs[key] = (time.monotonic(), result)
                            return result
                        if type(next_offset) is not int or next_offset <= offset:
                            raise SourceUnavailable("來源回傳了無效的分頁位置")
                        offset = next_offset
            except TimeoutError:
                raise SourceUnavailable("讀取來源任務逾時") from None
            raise SourceUnavailable("來源任務過多，無法完整載入")

    async def state(self, request):
        owner = self.management.owner(request)
        source_id = request.path_params["source_id"]
        source = self.source(owner, source_id)
        params = request.query_params
        if set(params) - {
            "q",
            "offset",
            "limit",
            "state",
            "runner",
            "project",
            "environment",
            "archived",
        }:
            raise ValueError("Unknown management query parameter")
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 60))
        query, state, runner = (
            params.get("q", "").strip().casefold(),
            params.get("state", "all"),
            params.get("runner", ""),
        )
        project = params.get("project", "")
        environment = params.get("environment", "")
        archived = params.get("archived", "active")
        if offset < 0 or not 1 <= limit <= 100 or len(query) > 2000:
            raise ValueError("Invalid task query or page")
        if state not in {"all", "unfinished", "active", "attention", "completed", "unconfirmed"}:
            raise ValueError("Invalid task state filter")
        if archived not in {"active", "archived", "all"}:
            raise ValueError("Invalid archive filter")
        if runner:
            identifier(runner)
        if project:
            identifier(project)
        if len(environment) > 100:
            raise ValueError("Invalid environment filter")
        try:
            data = (
                await self.remote_catalog(owner, source_id)
                if source
                else {
                    "tasks": self.local_catalog(owner),
                    "runners": self.management.runners(owner),
                    "observed_at": time.time(),
                }
            )
            catalog = data["tasks"]
            if source:
                runners = {
                    runner["id"]: runner
                    for runner in data["runners"]
                    if isinstance(runner.get("id"), str)
                }
                catalog = []
                for task in data["tasks"]:
                    runner_snapshot = runners.get(task["runner"])
                    # Earlier pages can precede a heartbeat received before the final page.
                    # Keep each task's work observation, but use the same Runner snapshot
                    # as the source header for its connection state.
                    if runner_snapshot is not None and type(runner_snapshot.get("online")) is bool:
                        task = {
                            **task,
                            "runner_online": runner_snapshot["online"],
                            "stale": not runner_snapshot["online"],
                            "liveness": {
                                **task.get("liveness", {}),
                                "runner_last_seen": runner_snapshot.get("last_seen"),
                                "runner_observed_at": data["observed_at"],
                                "data_freshness": "live"
                                if runner_snapshot["online"]
                                else "historical",
                            },
                        }
                    catalog.append(execution_freshness(task))
            projects = [
                {
                    "runner": task_runner,
                    "project": task_project,
                    "environment_label": self.environment_label(owner, source_id, task_runner),
                }
                for task_runner, task_project in sorted(
                    {(task["runner"], task["project"]) for task in catalog},
                    key=lambda pair: (pair[1], pair[0]),
                )
            ]
            archived_ids = self.archived_ids(owner, source_id)
            tasks = [
                task
                for task in catalog
                if (not runner or task.get("runner") == runner)
                and (not project or task["project"] == project)
                and (
                    not environment
                    or self.environment_label(owner, source_id, task["runner"]) == environment
                )
                and (archived == "all" or (task["id"] in archived_ids) == (archived == "archived"))
                and (
                    not query
                    or query
                    in " ".join(
                        str(task.get(k, ""))
                        for k in ("id", "title", "runner", "project", "summary")
                    ).casefold()
                )
                and (
                    state == "all"
                    or state == "completed"
                    and task["status"] == "completed"
                    or task["status"] != "completed"
                    and (
                        state == "unfinished"
                        or state == "active"
                        and priority(task) == 0
                        or state == "attention"
                        and (
                            task.get("_attention")
                            or task.get("state")
                            in {"waiting_operator", "pending_approval", "project_changed"}
                            or any(
                                job.get("status") == "pending_approval"
                                for job in task.get("jobs", [])
                            )
                        )
                        or state == "unconfirmed"
                        and (
                            task.get("_unconfirmed")
                            or task.get("liveness", {}).get("work", {}).get("unconfirmed_count")
                        )
                    )
                )
            ]

            def sort_key(task):
                activity = (task.get("liveness") or {}).get("conversation") or {}
                latest = (
                    task.get("_activity")
                    if source is None
                    else activity.get("finished_at") or activity.get("started_at")
                )
                updated = task.get("updated", 0)
                return priority(task), -(latest or updated), -updated, task["id"]

            tasks.sort(key=sort_key)
            selected = tasks[offset : offset + limit]
            if source is None:
                selected = [
                    self.management.describe(self.management.task(owner, task["id"]))
                    for task in selected
                ]
            return self.response(
                {
                    "source_id": source_id,
                    "source_label": self.label(owner, source_id),
                    "tasks": [
                        self.tag(owner, source_id, {**task, "archived": task["id"] in archived_ids})
                        for task in selected
                    ],
                    "projects": projects,
                    "runners": [self.tag(owner, source_id, runner) for runner in data["runners"]],
                    "next_offset": offset + limit if offset + limit < len(tasks) else None,
                    "total_count": len(tasks),
                    "observed_at": data["observed_at"],
                }
            )
        except SourceUnavailable as error:
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def archive(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        self.source(owner, source_id)
        identifier(task_id)
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"archived"}
            or type(body["archived"]) is not bool
        ):
            raise ValueError("Expected an archived boolean")
        # Save only this viewer's preference. It remains usable while the source is offline.
        with self.service.store.db:
            if body["archived"]:
                self.service.store.db.execute(
                    "INSERT INTO management_archives(owner,source_id,task_id,archived_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(owner,source_id,task_id) "
                    "DO UPDATE SET archived_at=excluded.archived_at",
                    (owner, source_id, task_id, time.time()),
                )
            else:
                self.service.store.db.execute(
                    "DELETE FROM management_archives WHERE owner=? AND source_id=? AND task_id=?",
                    (owner, source_id, task_id),
                )
        return self.response(
            {"source_id": source_id, "task_id": task_id, "archived": body["archived"]}
        )

    async def refresh(self, request):
        owner = self.management.owner(request)
        source_id = request.path_params["source_id"]
        source = self.source(owner, source_id)
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"task_id", "task_ids"}:
            raise ValueError("Expected task_id or task_ids")
        task_id, task_ids = body.get("task_id"), body.get("task_ids")
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
        if task_ids == []:
            return self.response(
                self.tag(owner, source_id, {"updates": [], "observed_at": time.time()})
            )
        if source is None:
            return self.response(
                self.tag(owner, source_id, await self.management.refresh_data(owner, body))
            )
        try:
            key = (owner, source_id)
            async with self.remote_client(owner, source_id) as client:
                data = await self.remote_request(client, "POST", "refresh", json=body)
            if data.get("updates"):
                async with self.locks.setdefault(key, asyncio.Lock()):
                    self.catalogs.pop(key, None)
            return self.response(self.tag(owner, source_id, data))
        except SourceUnavailable as error:
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def set_status(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        source = self.source(owner, source_id)
        identifier(task_id)
        if "write" not in self.service.config.clients[owner].permissions:
            return self.response({"error": "沒有更新任務的權限"}, 403)
        status = requested_status(await request.json())
        try:
            if source is None:
                try:
                    task = self.management.task(owner, task_id)
                except ValueError:
                    return self.response({"error": "任務不存在或無權存取"}, 404)
                if task["kind"] != "coding":
                    return self.response({"error": "專案管理紀錄需保持開啟"}, 400)
                updated = await self.management.update_status(owner, task_id, status)
            else:
                async with self.remote_client(owner, source_id) as client:
                    response = await client.post(
                        "/api/manage/tasks/" + task_id + "/status", json={"status": status}
                    )
                if response.status_code in {401, 403}:
                    return self.response({"error": "來源不允許更新這個任務"}, 403)
                if response.status_code == 404:
                    return self.response({"error": "找不到任務，或來源尚未支援狀態更新"}, 404)
                if response.status_code == 400:
                    return self.response({"error": "來源無法更新這個任務的狀態"}, 400)
                if not response.is_success:
                    raise SourceUnavailable("未取得更新結果，請重新整理確認狀態")
                updated = response.json()
                if (
                    not isinstance(updated, dict)
                    or updated.get("id") != task_id
                    or updated.get("status") != status
                    or type(updated.get("updated")) not in {int, float}
                    or not math.isfinite(updated["updated"])
                ):
                    raise SourceUnavailable("未取得更新結果，請重新整理確認狀態")
            return self.response(
                {
                    "source_id": source_id,
                    "task_id": task_id,
                    **{key: updated[key] for key in ("status", "updated")},
                }
            )
        except (SourceUnavailable, httpx.HTTPError, ValueError):
            return self.response({"error": "未取得來源回應，請重新整理確認任務狀態"}, 502)
        finally:
            # Invalidate even after a lost response: the source may already have saved the status.
            key = (owner, source_id)
            async with self.locks.setdefault(key, asyncio.Lock()):
                self.catalogs.pop(key, None)

    async def detail_data(self, owner: str, source_id: str, task_id: str) -> dict:
        identifier(task_id)
        if self.source(owner, source_id) is None:
            try:
                self.management.task(owner, task_id)
            except ValueError:
                raise TaskUnavailable("任務不存在或無權存取") from None
            return self.management.detail_data(owner, task_id)
        async with self.remote_client(owner, source_id) as client:
            data = await self.remote_request(client, "GET", "tasks/" + task_id)
        if data.get("id") != task_id:
            raise SourceUnavailable("來源回傳了不同的任務")
        self.validate_remote_task(data)
        if data.get("created_at") is None:
            try:
                data["created_at"] = await self.remote_creation_time(owner, source_id, task_id)
            except SourceUnavailable as error:
                data["date_error"] = str(error)
        return execution_freshness(data)

    async def detail(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        self.source(owner, source_id)
        try:
            return self.response(
                self.tag(owner, source_id, await self.detail_data(owner, source_id, task_id))
            )
        except TaskUnavailable as error:
            return self.response(
                {"source_id": source_id, "task_id": task_id, "error": str(error)}, 404
            )
        except SourceUnavailable as error:
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def handoff(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        source = self.source(owner, source_id)
        try:
            detail = await self.detail_data(owner, source_id, task_id)
            if source is None:
                task = self.management.task(owner, task_id)
                handoff = self.service.task_handoff(task, self.service.task_progress(task))
            else:
                handoff = {
                    "goal": detail["title"],
                    "summary": detail.get("summary", ""),
                    "plan": detail.get("plan", []),
                    "changed_files": [],
                    "validation": [],
                    "unread_outputs": detail.get("outputs", []),
                    "issues": detail.get("issues", []) + detail.get("errors", []),
                    "next_steps": [],
                    "updated_at": detail.get("updated"),
                    "files_observed_at": None,
                }
            instruction = (
                f"請使用「{self.label(owner, source_id)}」的 WebCodex 連線，在此對話接續原任務 {task_id}"
                f"（{detail['runner']}/{detail['project']}）。\n"
                f'先用 task_get(task_id="{task_id}") 讀取目前 progress 與 handoff，'
                f'再用 task_takeover(task_id="{task_id}") 接手並顯示任務卡片。'
                "沿用原 task_id、job_id 與 process_id，取得既有命令的最新狀態與輸出後繼續原工作。"
            )
            return self.response(
                {
                    "source_id": source_id,
                    "task_id": task_id,
                    "created_at": detail.get("created_at"),
                    "started_at": detail.get("started_at"),
                    "handoff": handoff,
                    "continuation_prompt": instruction,
                    "observed_at": detail.get("observed_at"),
                }
            )
        except TaskUnavailable as error:
            return self.response(
                {"source_id": source_id, "task_id": task_id, "error": str(error)}, 404
            )
        except SourceUnavailable as error:
            return self.response({"source_id": source_id, "error": str(error)}, 502)


def source_routes(sources: Sources) -> list[Route]:
    return [
        Route("/api/manage/sources", sources.listing, methods=["GET"]),
        Route("/api/manage/sources/{source_id}/state", sources.state, methods=["GET"]),
        Route("/api/manage/sources/{source_id}/refresh", sources.refresh, methods=["POST"]),
        Route("/api/manage/sources/{source_id}/tasks/{task_id}", sources.detail, methods=["GET"]),
        Route(
            "/api/manage/sources/{source_id}/tasks/{task_id}/status",
            sources.set_status,
            methods=["POST"],
        ),
        Route(
            "/api/manage/sources/{source_id}/tasks/{task_id}/archive",
            sources.archive,
            methods=["POST"],
        ),
        Route(
            "/api/manage/sources/{source_id}/tasks/{task_id}/handoff",
            sources.handoff,
            methods=["GET"],
        ),
    ]
