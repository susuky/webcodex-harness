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
from .connections import REASONS, public_details, safe_details
from .management import PROCESS_FRESH_SECONDS, execution_freshness, requested_status
from .store import INTERNAL_OBSERVATION_PREFIXES, TaskArchived


class SourceUnavailable(Exception):
    def __init__(self, message, reason="source_invalid_response", **details):
        super().__init__(message)
        self.reason, self.details = reason, details

    @classmethod
    def transport(cls, error, message, timeout_seconds=15):
        reason = (
            "source_connect_timeout"
            if isinstance(error, httpx.ConnectTimeout)
            else "source_read_timeout"
            if isinstance(error, httpx.ReadTimeout)
            else "source_timeout"
            if isinstance(error, httpx.TimeoutException)
            else "source_invalid_response"
            if isinstance(error, (httpx.ProtocolError, httpx.DecodingError, ValueError))
            else "source_unreachable"
        )
        details = {"exception_type": type(error).__name__}
        if isinstance(error, httpx.TimeoutException):
            details["timeout_seconds"] = timeout_seconds
        return cls(message, reason, **details)


class TaskUnavailable(Exception):
    pass


def priority(task: dict) -> int:
    if task.get("archived"):
        return 4
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
    if task.get("unanswered_operator_questions") or any(
        job.get("status") == "pending_approval" for job in jobs
    ):
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
        self.connection_cache: dict[tuple[str, str, bool], tuple[float, dict]] = {}
        self.connection_locks: dict[tuple[str, str, bool], asyncio.Lock] = {}
        # Existing preferences stay here until the task's source records the archive.
        with self.service.store.db:
            self.service.store.db.execute(
                "CREATE TABLE IF NOT EXISTS management_archives ("
                "owner TEXT NOT NULL,source_id TEXT NOT NULL,task_id TEXT NOT NULL,"
                "archived_at REAL NOT NULL,PRIMARY KEY(owner,source_id,task_id))"
            )
        for row in self.service.store.rows(
            "SELECT a.owner,a.task_id,a.archived_at FROM management_archives a "
            "JOIN tasks t ON t.id=a.task_id AND t.owner=a.owner WHERE a.source_id='local'"
        ):
            self.service.set_task_archive(row["owner"], row["task_id"], row["archived_at"])
            self.forget_legacy_archive(row["owner"], "local", row["task_id"])

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

    def forget_legacy_archive(self, owner: str, source_id: str, task_id: str):
        with self.service.store.db:
            self.service.store.db.execute(
                "DELETE FROM management_archives WHERE owner=? AND source_id=? AND task_id=?",
                (owner, source_id, task_id),
            )

    def tag(self, owner: str, source_id: str, row: dict) -> dict:
        result = {**row, "source_id": source_id, "source_label": self.label(owner, source_id)}
        if "project" in row:
            result["environment_label"] = self.environment_label(owner, source_id, row["runner"])
        if "project" in row and row.get("created_at") is None:
            created = self.creation_times.get((owner, source_id, row["id"]))
            if created is not None:
                result["created_at"] = created
        return result

    @staticmethod
    def archive_flags(task: dict, legacy_ids: set[str]) -> dict:
        pending = task["id"] in legacy_ids and not task.get("archived")
        return {"archived": bool(task.get("archived")) or pending, "archive_pending": pending}

    async def remote_archive(self, client, task_id: str, archived: bool):
        data = await self.remote_request(
            client, "POST", f"tasks/{task_id}/archive", json={"archived": archived}
        )
        if data.get("task_id") != task_id or data.get("archived") is not archived:
            raise SourceUnavailable("未取得來源的封存確認")

    async def sync_legacy_archives(self, owner: str, source_id: str, client, tasks: list):
        if "write" not in self.service.config.clients[owner].permissions:
            return
        legacy = self.archived_ids(owner, source_id)
        if not legacy:
            return
        for task in tasks:
            if task["id"] not in legacy or "archived" not in task:
                continue
            if not task["archived"]:
                try:
                    await self.remote_archive(client, task["id"], True)
                except SourceUnavailable:
                    return
                task["archived"] = True
            self.forget_legacy_archive(owner, source_id, task["id"])

    @staticmethod
    def response(data: dict, status_code: int = 200):
        return JSONResponse(data, status_code, headers={"Cache-Control": "no-store"})

    async def source_failed(self, owner, source_id, operation, error, started):
        if source_id != "local":
            await asyncio.to_thread(
                self.service.connections.fail,
                "source",
                operation,
                error.reason,
                source_id=source_id,
                owner=owner,
                details={
                    "operation": operation,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    **error.details,
                },
            )

    async def source_recovered(self, owner, source_id, operation):
        await asyncio.to_thread(
            self.service.connections.recover,
            "source",
            operation,
            source_id=source_id,
            owner=owner,
        )

    async def connections(self, request):
        owner = self.management.owner(request)
        if set(request.query_params) - {"limit", "evidence"}:
            raise ValueError("Unknown connection history parameter")
        limit = int(request.query_params.get("limit", 30))
        if not 1 <= limit <= 100:
            raise ValueError("Connection history limit must be between 1 and 100")
        evidence = request.query_params.get("evidence", "0")
        if evidence not in {"0", "1"}:
            raise ValueError("Expected evidence=0 or evidence=1")
        data = await self.connection_data(
            owner,
            request.path_params.get("source_id"),
            limit=limit,
            include_evidence=evidence == "1",
        )
        return self.response(data)

    async def connection_data(
        self, owner: str, source_id: str | None = None, *, limit=30, include_evidence=False
    ):
        client = self.service.config.clients.get(owner)
        if client is None or "read" not in client.permissions:
            raise ValueError("Read permission is required")
        if source_id:
            self.source(owner, source_id)
            source_ids = {source_id}
        else:
            source_ids = {"local", *self.service.config.clients[owner].management_sources}
        runner_ids = {
            project.split("/", 1)[0]
            for project in [
                *self.service.allowed_projects(owner),
                *self.service.config.clients[owner].project_roots,
            ]
        }
        data = await asyncio.to_thread(
            self.service.connections.snapshot,
            owner=owner,
            source_ids=source_ids,
            runner_ids=runner_ids,
            limit=limit,
            include_evidence=include_evidence,
        )
        for incident in data["incidents"]:
            incident["source_label"] = self.label(owner, incident["source_id"])
        data["sources"] = []
        data["runners"] = []
        if "local" in source_ids:
            data["sources"].append(
                {
                    "source_id": "local",
                    "source_label": "本機",
                    "status": "available",
                    "observed_at": data["observed_at"],
                }
            )
            data["runners"] = [
                self.tag(owner, "local", runner) for runner in self.management.runners(owner)
            ]
        remote_ids = sorted(source_ids - {"local"})
        remote_data = await asyncio.gather(
            *(
                self.remote_connections(owner, remote, include_evidence=include_evidence)
                for remote in remote_ids
            )
        )
        for remote, result in zip(remote_ids, remote_data, strict=True):
            data["sources"].append(
                {"source_id": remote, "source_label": self.label(owner, remote), **result["source"]}
            )
            for row in result.get("incidents", []):
                data["incidents"].append(
                    {**row, "source_id": remote, "source_label": self.label(owner, remote)}
                )
            for row in result.get("runners", []):
                data["runners"].append(
                    self.tag(
                        owner,
                        remote,
                        {
                            **row,
                            "observed_at": result["source"].get("observed_at"),
                            "stale": result["source"]["status"] != "available",
                        },
                    )
                )
        data["incidents"].sort(
            key=lambda row: row["recovered_at"] or row.get("closed_at") or row["started_at"],
            reverse=True,
        )
        data["incidents"] = data["incidents"][:limit]
        return data

    async def remote_connections(self, owner, source_id, *, include_evidence=False):
        key = (owner, source_id, include_evidence)
        async with self.connection_locks.setdefault(key, asyncio.Lock()):
            cached = self.connection_cache.get(key)
            if cached and time.monotonic() - cached[0] < 15:
                return cached[1]
            result = {
                **(cached[1] if cached else {}),
                "source": {
                    "status": "unavailable",
                    "observed_at": cached[1]["source"].get("observed_at") if cached else None,
                },
            }
            try:
                async with self.remote_client(owner, source_id) as client:
                    response = await client.get(
                        "/api/manage/sources/local/connections",
                        params={"evidence": "1"} if include_evidence else None,
                    )
                if response.status_code == 404:
                    result["source"] = {"status": "unsupported"}
                elif response.status_code in {401, 403}:
                    result["source"]["reason"] = "source_auth_failed"
                elif not response.is_success:
                    result["source"].update(
                        reason="source_http_error", http_status=response.status_code
                    )
                else:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("incidents"), list
                    ):
                        raise ValueError("Invalid connection history")
                    incidents = []
                    for row in payload["incidents"][:100]:
                        if not isinstance(row, dict) or row.get("source_id") != "local":
                            continue
                        reason = row.get("reason")
                        if reason not in REASONS or row.get("component") not in {
                            "server",
                            "source",
                            "runner",
                        }:
                            raise ValueError("Invalid connection incident")
                        started, recovered = row.get("started_at"), row.get("recovered_at")
                        closed = row.get("closed_at")
                        if any(
                            type(value) not in (int, float) or not math.isfinite(value)
                            for value in [
                                started,
                                *([recovered] if recovered is not None else []),
                                *([closed] if closed is not None else []),
                            ]
                        ):
                            raise ValueError("Invalid connection incident time")
                        if any(
                            not isinstance(row.get(field, {}), dict)
                            for field in ("details", "recovery_details")
                        ):
                            raise ValueError("Invalid connection incident details")
                        filter_details = safe_details if include_evidence else public_details
                        details = filter_details(row.get("details"))
                        incidents.append(
                            {
                                "id": str(row.get("id", ""))[:64],
                                "component": row["component"],
                                "source_id": source_id,
                                "runner_id": row.get("runner_id"),
                                "started_at": started,
                                "recovered_at": recovered,
                                **(
                                    {"closed_at": closed, "end_reason": "diagnostic_changed"}
                                    if closed is not None
                                    else {}
                                ),
                                "last_seen_at": started,
                                "reason": reason,
                                "reason_label": REASONS[reason],
                                "details": details,
                                "recovery_details": filter_details(row.get("recovery_details")),
                                "state": "changed"
                                if closed is not None
                                else "active"
                                if recovered is None
                                else "recovered",
                            }
                        )
                    runners = payload.get("runners", [])
                    if not isinstance(runners, list) or any(
                        not isinstance(row, dict) for row in runners
                    ):
                        raise ValueError("Invalid Runner connection history")
                    result = {
                        "source": {
                            "status": "available",
                            "observed_at": time.time(),
                            "persisted": payload.get("persisted") is not False,
                        },
                        "incidents": incidents,
                        "runners": [
                            {
                                "id": row["id"],
                                "last_seen": row.get("last_seen"),
                                "online": row.get("online") is True,
                            }
                            for row in runners
                            if isinstance(row.get("id"), str)
                        ],
                    }
            except httpx.ConnectTimeout:
                result["source"]["reason"] = "source_connect_timeout"
            except httpx.ReadTimeout:
                result["source"]["reason"] = "source_read_timeout"
            except httpx.TimeoutException:
                result["source"]["reason"] = "source_timeout"
            except httpx.HTTPError:
                result["source"]["reason"] = "source_unreachable"
            except (ValueError, TypeError):
                result["source"]["reason"] = "source_invalid_response"
            self.connection_cache[key] = (time.monotonic(), result)
            return result

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
            "(archived_at IS NOT NULL) AS archived,"
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
        observations = self.service.store.rows(
            "SELECT task_id,status,observed_at AS updated FROM process_states "
            f"WHERE task_id IN ({slots}) AND status='running'",
            tuple(by_id),
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

    async def remote_request(self, client, method: str, path: str, **kwargs) -> dict | None:
        try:
            response = await client.request(method, "/api/manage/" + path, **kwargs)
            if (
                method == "GET"
                and path == "sources/local/state"
                and (
                    response.status_code == 404
                    or response.status_code == 400
                    and response.json() == {"error": "Unknown management query parameter"}
                )
            ):
                return None  # Older sources need the unfiltered catalog route.
            if response.status_code in {401, 403}:
                raise SourceUnavailable(
                    "來源驗證失敗或權限已撤銷",
                    "source_auth_failed",
                    http_status=response.status_code,
                )
            if method == "GET" and path.startswith("tasks/") and response.status_code in {400, 404}:
                raise TaskUnavailable("任務不存在或無權存取")
            if not response.is_success:
                raise SourceUnavailable(
                    f"來源回應錯誤（HTTP {response.status_code}）",
                    "source_http_error",
                    http_status=response.status_code,
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected an object")
            return result
        except httpx.ConnectTimeout:
            raise SourceUnavailable(
                "連線至來源逾時", "source_connect_timeout", timeout_seconds=15
            ) from None
        except httpx.ReadTimeout:
            raise SourceUnavailable(
                "來源回應逾時", "source_read_timeout", timeout_seconds=15
            ) from None
        except httpx.TimeoutException:
            raise SourceUnavailable("來源通訊逾時", "source_timeout", timeout_seconds=15) from None
        except (httpx.ProtocolError, httpx.DecodingError) as error:
            raise SourceUnavailable(
                "來源回傳了無效資料", "source_invalid_response", exception_type=type(error).__name__
            ) from None
        except httpx.HTTPError:
            raise SourceUnavailable("無法連線至來源", "source_unreachable") from None
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
            started = time.monotonic()
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
                if response.status_code in {401, 403}:
                    raise SourceUnavailable(
                        "讀取任務日期時來源驗證失敗",
                        "source_auth_failed",
                        http_status=response.status_code,
                    )
                if not response.is_success:
                    raise SourceUnavailable(
                        f"讀取任務日期失敗（HTTP {response.status_code}）",
                        "source_http_error",
                        http_status=response.status_code,
                    )
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
            except SourceUnavailable as error:
                await self.source_failed(owner, source_id, "task-date", error, started)
                raise
            except (httpx.HTTPError, ValueError) as error:
                failure = SourceUnavailable.transport(error, "讀取任務日期失敗", timeout_seconds=3)
                await self.source_failed(owner, source_id, "task-date", failure, started)
                raise failure from None
            if len(self.creation_times) >= 4096:
                self.creation_times.pop(next(iter(self.creation_times)))
            self.creation_times[key] = created
            await self.source_recovered(owner, source_id, "task-date")
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
        if "archived" in task and type(task["archived"]) is not bool:
            raise SourceUnavailable("來源回傳了無效的封存狀態")
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
        unanswered = task.get("unanswered_operator_questions", 0)
        if type(unanswered) is not int or unanswered < 0:
            raise SourceUnavailable("來源回傳了無效的工作狀態")
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

    async def remote_window(self, owner, source_id, params, window):
        source = self.source(owner, source_id)
        params = dict(params)
        environment = params.pop("environment", "")
        if environment and not params.get("runner") and environment != self.label(owner, source_id):
            matching = [key for key, label in source.runner_labels.items() if label == environment]
            if len(matching) == 1:
                params["runner"] = matching[0]

        try:
            async with asyncio.timeout(30), self.remote_client(owner, source_id) as client:
                path = "sources/local/state"
                try:
                    data = await self.remote_request(client, "GET", path, params=params)
                except SourceUnavailable as error:
                    if (
                        error.reason
                        not in {
                            "source_connect_timeout",
                            "source_read_timeout",
                            "source_unreachable",
                        }
                        and error.details.get("exception_type") != "RemoteProtocolError"
                    ):
                        raise
                    data = await self.remote_request(
                        client, "GET", path, params=params, headers={"Connection": "close"}
                    )
        except TimeoutError:
            raise SourceUnavailable(
                "讀取來源任務逾時", "source_timeout", timeout_seconds=30
            ) from None
        if data is None:
            return None

        total, observed = data.get("total_count"), data.get("observed_at")
        if (
            type(total) is not int
            or total < 0
            or type(observed) not in {int, float}
            or not math.isfinite(observed)
            or not isinstance(data.get("tasks"), list)
            or len(data["tasks"]) != min(window, total)
            or data.get("next_offset") != (window if window < total else None)
            or not isinstance(data.get("runners"), list)
            or any(not isinstance(row, dict) for row in data["runners"])
            or not isinstance(data.get("projects"), list)
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("runner"), str)
                or not isinstance(row.get("project"), str)
                for row in data["projects"]
            )
        ):
            raise SourceUnavailable("來源回傳了無效的任務清單")
        for task in data["tasks"]:
            self.validate_remote_task(task)
        if len({task["id"] for task in data["tasks"]}) != len(data["tasks"]):
            raise SourceUnavailable("來源回傳了無效的任務清單")
        projects = [
            {**row, "environment_label": self.environment_label(owner, source_id, row["runner"])}
            for row in data["projects"]
        ]
        tasks = [self.tag(owner, source_id, task) for task in data["tasks"]]
        if environment and any(
            row["environment_label"] != environment
            for row in projects
            if (not params.get("runner") or params["runner"] == row["runner"])
            and (not params.get("project") or params["project"] == row["project"])
        ):
            if len(tasks) < total:
                return None  # An environment can combine multiple Runners into one page.
            tasks = [task for task in tasks if task["environment_label"] == environment]
            total = len(tasks)
        await self.source_recovered(owner, source_id, "state")
        return {
            "source_id": source_id,
            "source_label": self.label(owner, source_id),
            "tasks": tasks,
            "projects": projects,
            "runners": [self.tag(owner, source_id, runner) for runner in data["runners"]],
            "next_offset": data.get("next_offset"),
            "total_count": total,
            "observed_at": observed,
        }

    async def remote_catalog(self, owner: str, source_id: str) -> dict:
        key = (owner, source_id)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.catalogs.get(key)
            if cached and time.monotonic() - cached[0] < 3:
                return cached[1]
            tasks, seen, offset = [], set(), 0
            catalog_timeout = 30
            try:
                async with (
                    asyncio.timeout(catalog_timeout),
                    self.remote_client(owner, source_id) as client,
                ):
                    # New sources keep catalog order stable across updates. Older ones ignore the flag.
                    for _ in range(100):
                        page = await self.remote_request(
                            client, "GET", "state", params={"offset": offset, "catalog": 1}
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
                            try:
                                async with asyncio.timeout(3):
                                    await self.sync_legacy_archives(owner, source_id, client, tasks)
                            except TimeoutError:
                                pass  # Keep the pending archives for the next successful sync.
                            result = {**page, "tasks": tasks, "next_offset": None}
                            self.catalogs[key] = (time.monotonic(), result)
                            await self.source_recovered(owner, source_id, "state")
                            return result
                        if type(next_offset) is not int or next_offset <= offset:
                            raise SourceUnavailable("來源回傳了無效的分頁位置")
                        offset = next_offset
            except TimeoutError:
                raise SourceUnavailable(
                    "讀取來源任務逾時", "source_timeout", timeout_seconds=catalog_timeout
                ) from None
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
            "window",
            "state",
            "runner",
            "project",
            "environment",
            "archived",
        }:
            raise ValueError("Unknown management query parameter")
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 60))
        window = int(params["window"]) if "window" in params else None
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
        if window is not None:
            if window < 1 or "offset" in params or "limit" in params:
                raise ValueError("Use a positive window size without offset or limit")
            limit = window
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
        started = time.monotonic()
        try:
            if source and window is not None and not self.archived_ids(owner, source_id):
                # Let the source filter and describe just the task center's visible window.
                page = await self.remote_window(owner, source_id, params, window)
                if page is not None:
                    return self.response(page)
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
                legacy_ids = self.archived_ids(owner, source_id)
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
                    catalog.append(
                        execution_freshness({**task, **self.archive_flags(task, legacy_ids)})
                    )
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
            tasks = [
                task
                for task in catalog
                if (not runner or task.get("runner") == runner)
                and (not project or task["project"] == project)
                and (
                    not environment
                    or self.environment_label(owner, source_id, task["runner"]) == environment
                )
                and (archived == "all" or bool(task["archived"]) == (archived == "archived"))
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
                        or not task["archived"]
                        and state == "attention"
                        and (
                            task.get("_attention")
                            or task.get("unanswered_operator_questions")
                            or task.get("state")
                            in {"waiting_operator", "pending_approval", "project_changed"}
                            or any(
                                job.get("status") == "pending_approval"
                                for job in task.get("jobs", [])
                            )
                        )
                        or not task["archived"]
                        and state == "unconfirmed"
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
                described = []
                for index, task in enumerate(selected):
                    if index and index % 60 == 0:
                        await asyncio.sleep(0)
                    described.append(
                        self.management.describe(self.management.task(owner, task["id"]))
                    )
                selected = described
            return self.response(
                {
                    "source_id": source_id,
                    "source_label": self.label(owner, source_id),
                    "tasks": [self.tag(owner, source_id, task) for task in selected],
                    "projects": projects,
                    "runners": [self.tag(owner, source_id, runner) for runner in data["runners"]],
                    "next_offset": offset + limit if offset + limit < len(tasks) else None,
                    "total_count": len(tasks),
                    "observed_at": data["observed_at"],
                }
            )
        except SourceUnavailable as error:
            await self.source_failed(owner, source_id, "state", error, started)
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def archive(self, request):
        owner = self.management.owner(request)
        source_id, task_id = (
            request.path_params.get("source_id", "local"),
            request.path_params["task_id"],
        )
        source = self.source(owner, source_id)
        identifier(task_id)
        if "write" not in self.service.config.clients[owner].permissions:
            return self.response({"error": "沒有封存或還原任務的權限"}, 403)
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"archived"}
            or type(body["archived"]) is not bool
        ):
            raise ValueError("Expected an archived boolean")
        archived = body["archived"]
        if source is None:
            try:
                self.management.task(owner, task_id)
            except ValueError:
                return self.response({"error": "任務不存在或無權存取"}, 404)
            self.service.set_task_archive(owner, task_id, time.time() if archived else None)
        else:
            key = (owner, source_id)
            async with self.locks.setdefault(key, asyncio.Lock()):
                try:
                    async with self.remote_client(owner, source_id) as client:
                        await self.remote_archive(client, task_id, archived)
                except SourceUnavailable as error:
                    status = error.details.get("http_status")
                    if status in {401, 403}:
                        return self.response({"error": "來源未授權封存或還原"}, 403)
                    if status == 404:
                        return self.response({"error": "任務不存在或來源尚未支援封存"}, 404)
                    return self.response({"error": "未取得來源確認，請重新整理任務狀態"}, 502)
                finally:
                    self.catalogs.pop(key, None)
                self.forget_legacy_archive(owner, source_id, task_id)
        return self.response({"source_id": source_id, "task_id": task_id, "archived": archived})

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
        if source:
            legacy_ids = self.archived_ids(owner, source_id)
            if task_id in legacy_ids:
                task_ids = []
            elif task_ids is not None:
                task_ids = [value for value in task_ids if value not in legacy_ids]
            if task_ids is not None:
                body = {"task_ids": task_ids}
        if task_ids == []:
            return self.response(
                self.tag(owner, source_id, {"updates": [], "observed_at": time.time()})
            )
        if source is None:
            return self.response(
                self.tag(owner, source_id, await self.management.refresh_data(owner, body))
            )
        started = time.monotonic()
        try:
            key = (owner, source_id)
            async with self.remote_client(owner, source_id) as client:
                data = await self.remote_request(client, "POST", "refresh", json=body)
            if data.get("updates"):
                async with self.locks.setdefault(key, asyncio.Lock()):
                    self.catalogs.pop(key, None)
            await self.source_recovered(owner, source_id, "refresh")
            return self.response(self.tag(owner, source_id, data))
        except SourceUnavailable as error:
            await self.source_failed(owner, source_id, "refresh", error, started)
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def set_status(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        source = self.source(owner, source_id)
        identifier(task_id)
        if "write" not in self.service.config.clients[owner].permissions:
            return self.response({"error": "沒有更新任務的權限"}, 403)
        status = requested_status(await request.json())
        if source and task_id in self.archived_ids(owner, source_id):
            return self.response({"error": "請先還原任務"}, 409)
        started = time.monotonic()
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
                    await self.source_failed(
                        owner,
                        source_id,
                        "status",
                        SourceUnavailable(
                            "來源不允許更新這個任務",
                            "source_auth_failed",
                            http_status=response.status_code,
                        ),
                        started,
                    )
                    return self.response({"error": "來源不允許更新這個任務"}, 403)
                if response.status_code == 404:
                    return self.response({"error": "找不到任務，或來源尚未支援狀態更新"}, 404)
                if response.status_code == 409:
                    return self.response({"error": "請先還原任務"}, 409)
                if response.status_code == 400:
                    return self.response({"error": "來源無法更新這個任務的狀態"}, 400)
                if not response.is_success:
                    raise SourceUnavailable(
                        "未取得更新結果，請重新整理確認狀態",
                        "source_http_error",
                        http_status=response.status_code,
                    )
                updated = response.json()
                if (
                    not isinstance(updated, dict)
                    or updated.get("id") != task_id
                    or updated.get("status") != status
                    or type(updated.get("updated")) not in {int, float}
                    or not math.isfinite(updated["updated"])
                ):
                    raise SourceUnavailable("未取得更新結果，請重新整理確認狀態")
                await self.source_recovered(owner, source_id, "status")
            return self.response(
                {
                    "source_id": source_id,
                    "task_id": task_id,
                    **{key: updated[key] for key in ("status", "updated")},
                }
            )
        except TaskArchived:
            return self.response({"error": "請先還原任務"}, 409)
        except (SourceUnavailable, httpx.HTTPError, ValueError) as error:
            if source:
                if not isinstance(error, SourceUnavailable):
                    error = SourceUnavailable.transport(error, "未取得來源回應")
                await self.source_failed(owner, source_id, "status", error, started)
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
        await self.source_recovered(owner, source_id, "tasks")
        return execution_freshness(
            {**data, **self.archive_flags(data, self.archived_ids(owner, source_id))}
        )

    async def detail(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        self.source(owner, source_id)
        started = time.monotonic()
        try:
            return self.response(
                self.tag(owner, source_id, await self.detail_data(owner, source_id, task_id))
            )
        except TaskUnavailable as error:
            return self.response(
                {"source_id": source_id, "task_id": task_id, "error": str(error)}, 404
            )
        except SourceUnavailable as error:
            await self.source_failed(owner, source_id, "tasks", error, started)
            return self.response({"source_id": source_id, "error": str(error)}, 502)

    async def handoff(self, request):
        owner = self.management.owner(request)
        source_id, task_id = request.path_params["source_id"], request.path_params["task_id"]
        source = self.source(owner, source_id)
        started = time.monotonic()
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
                None
                if detail["archived"]
                else (
                    f"請使用「{self.label(owner, source_id)}」的 WebCodex 連線，在此對話接續原任務 {task_id}"
                    f"（{detail['runner']}/{detail['project']}）。\n"
                    f'直接呼叫 task_takeover(task_id="{task_id}") 取得進度與交接資料，'
                    "依回傳的 next_action 顯示任務卡片。"
                    "沿用原 task_id、job_id 與 process_id，取得既有命令的最新狀態與輸出後繼續原工作。"
                )
            )
            return self.response(
                {
                    "source_id": source_id,
                    "task_id": task_id,
                    "archived": detail["archived"],
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
            await self.source_failed(owner, source_id, "tasks", error, started)
            return self.response({"source_id": source_id, "error": str(error)}, 502)


def source_routes(sources: Sources) -> list[Route]:
    return [
        Route("/api/manage/connections", sources.connections, methods=["GET"]),
        Route("/api/manage/sources", sources.listing, methods=["GET"]),
        Route("/api/manage/tasks/{task_id}/archive", sources.archive, methods=["POST"]),
        Route("/api/manage/sources/{source_id}/connections", sources.connections, methods=["GET"]),
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
