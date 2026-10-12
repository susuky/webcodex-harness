from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3

import httpx

from .catalog import OutcomeUnknown, definitions
from .computer import Computer
from .config import RunnerConfig, identifier, private_dir, project_binding
from .extensions import Extensions
from .filesystem import Workspace
from .processes import Processes
from .projects import PROJECT_OPERATIONS, ProjectOperations, restore_projects, root_inventory
from .runtime import Runtime
from .store import new_id

logger = logging.getLogger(__name__)
DEFINITIONS = definitions(Runtime, remote=True)


class Runner:
    def __init__(self, config: RunnerConfig):
        self.config = config = config.model_copy(deep=True)
        private_dir(config.state_dir)
        self.lock_file = (config.state_dir / "runner.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lock_file.close()
            raise ValueError("A Runner is already using this state directory") from error
        self.instance = new_id()
        self.db = sqlite3.connect(config.state_dir / "deliveries.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
            id TEXT PRIMARY KEY, instance TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, acknowledged INTEGER NOT NULL DEFAULT 0
        )""")
        self.db.execute(
            "UPDATE deliveries SET status='unknown',result=? WHERE status='running'",
            (
                json.dumps(
                    {"error": "Runner restarted during execution; inspect state before retrying"}
                ),
            ),
        )
        self.db.commit()
        try:
            restore_projects(config, self.db)
        except Exception:
            self.db.close()
            self.lock_file.close()
            raise
        self.processes = Processes(config)
        self.computer = Computer()
        self.extensions = {project: Extensions(config, project) for project in config.projects}
        self.locks = {project: asyncio.Lock() for project in config.projects}
        self.active: dict[str, asyncio.Task] = {}
        self.stop = asyncio.Event()
        self.delivery_lock = asyncio.Lock()
        self.project_lock = asyncio.Lock()

    def inventory(self) -> dict:
        """One removed/replaced directory must not disconnect all other projects."""
        roots = root_inventory(self.config, self.db)
        managed = {row[0] for row in self.db.execute("SELECT name FROM managed_projects")}
        available = {name for root in roots.values() for name in root["projects"]}
        result = {}
        for name, path in self.config.projects.items():
            if name in managed and name not in available:
                continue
            try:
                result[name] = project_binding(path)
            except (ValueError, OSError):
                continue
        return result

    async def execute(self, job: dict):
        job_id = identifier(job["id"])
        if self.db.execute("SELECT 1 FROM deliveries WHERE id=?", (job_id,)).fetchone():
            return  # An acknowledged claim is never executed twice.
        self.db.execute(
            "INSERT INTO deliveries(id,instance,status) VALUES (?,?,'running')",
            (job_id, job["instance"]),
        )
        self.db.commit()
        status = "succeeded"
        try:
            name, project = job["tool"], job["project"]
            management = job.get("kind", "coding") == "project"
            definition = (PROJECT_OPERATIONS if management else DEFINITIONS).get(name)
            if not definition or definition.permission not in self.config.permissions:
                raise ValueError("Tool is not permitted by this Runner")
            if management:
                async with self.project_lock:
                    root = self.config.project_roots.get(project)
                    if not root or project_binding(root) != job["binding"]:
                        raise ValueError("Project root binding does not match this Runner")
                    args = definition.model.model_validate(job["arguments"]).model_dump()
                    if args["root"] != self.config.runner_id + "/" + project:
                        raise ValueError("Project operation targets a different root")
                    operations = ProjectOperations(
                        self.config, self.db, self.processes, job["task_id"], job["binding"]
                    )
                    result = await getattr(operations, name)(**args)
            else:
                if self.inventory().get(project) != job["binding"]:
                    raise ValueError(
                        "This project's directory is unavailable or changed. Use projects_list to "
                        "select an available project; other projects remain usable."
                    )
                record = self.db.execute(
                    "SELECT root,root_binding FROM managed_projects WHERE name=?", (project,)
                ).fetchone()
                if (
                    record
                    and project_binding(self.config.project_roots[record["root"]])
                    != record["root_binding"]
                ):
                    raise ValueError("Managed project's parent root changed")
                args = definition.model.model_validate(
                    {**job["arguments"], "task_id": job["task_id"]}
                ).model_dump()
                args.pop("task_id")
                args.pop("request_id")
                if project not in self.extensions:
                    self.extensions[project] = Extensions(self.config, project)
                    self.locks[project] = asyncio.Lock()
                runtime = Runtime(
                    self.config,
                    Workspace(self.config.projects[project], expected_binding=job["binding"]),
                    self.processes,
                    self.extensions[project],
                    job["task_id"],
                    job_id,
                    self.computer,
                )
                if definition.permission in {"write", "git_write", "extensions"}:
                    async with self.locks[project]:
                        result = await getattr(runtime, name)(**args)
                else:
                    result = await getattr(runtime, name)(**args)
            if len(json.dumps(result, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
                status, result = (
                    "unknown",
                    {"error": "Result exceeded the transport limit; inspect state before retrying"},
                )
        except asyncio.CancelledError:
            status, result = (
                "unknown",
                {"error": "Runner stopped during execution; inspect state before retrying"},
            )
        except Exception as error:
            status = "unknown" if isinstance(error, OutcomeUnknown) else "failed"
            result = {"error": str(error)[:2000], "error_type": type(error).__name__}
        self.db.execute(
            "UPDATE deliveries SET status=?,result=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), job_id),
        )
        self.db.commit()

    async def register(self, client: httpx.AsyncClient, *, refresh_inventory: bool = False):
        recorded = [
            r[0]
            for r in self.db.execute("SELECT id FROM deliveries WHERE acknowledged=0 LIMIT 10000")
        ]
        known = list(dict.fromkeys([*self.active, *recorded]))[:10000]
        response = await client.post(
            "/api/runner/register",
            json={
                "instance": self.instance,
                "projects": self.inventory(),
                "project_roots": root_inventory(self.config, self.db),
                "known_jobs": known,
                "refresh_inventory": refresh_inventory,
            },
        )
        response.raise_for_status()

    async def flush(self, client: httpx.AsyncClient):
        async with self.delivery_lock:
            await self._flush(client)

    async def _flush(self, client: httpx.AsyncClient):
        for row in list(
            self.db.execute(
                "SELECT * FROM deliveries WHERE acknowledged=0 AND status!='running' LIMIT 100"
            )
        ):
            result = json.loads(row["result"])
            if row["status"] == "succeeded" and result.get("registered") is True:
                # Publish the durable project inventory before acknowledging creation to the client.
                await self.register(client, refresh_inventory=True)
            response = await client.post(
                "/api/runner/complete",
                json={
                    "job_id": row["id"],
                    "instance": row["instance"],
                    "status": row["status"],
                    "result": result,
                },
            )
            response.raise_for_status()
            self.db.execute("UPDATE deliveries SET acknowledged=1 WHERE id=?", (row["id"],))
            self.db.commit()

    async def execute_and_deliver(self, job: dict, client: httpx.AsyncClient):
        await self.execute(job)
        try:
            await self.flush(client)
        except (httpx.HTTPError, ValueError, OSError):
            logger.info("Result delivery deferred until the connection recovers")

    async def run(self):
        registered = False
        async with httpx.AsyncClient(
            base_url=self.config.server_url,
            headers={"Authorization": "Bearer " + self.config.token},
            timeout=10,
            trust_env=False,
        ) as client:
            try:
                while not self.stop.is_set():
                    try:
                        if not registered:
                            await self.register(client)
                            registered = True
                            logger.info("Runner %s connected", self.config.runner_id)
                        await self.flush(client)
                        self.active = {
                            key: task for key, task in self.active.items() if not task.done()
                        }
                        free = self.config.max_concurrent_jobs - len(self.active)
                        response = await client.post(
                            "/api/runner/poll",
                            json={
                                "instance": self.instance,
                                "limit": free,
                            },
                        )
                        response.raise_for_status()
                        for job in response.json()["jobs"]:
                            if job["id"] not in self.active:
                                self.active[job["id"]] = asyncio.create_task(
                                    self.execute_and_deliver(job, client)
                                )
                        if not free:
                            await asyncio.sleep(0.2)
                    except (httpx.HTTPError, ValueError, KeyError, OSError) as error:
                        registered = False
                        logger.warning(
                            "Runner connection unavailable (%s); retrying with existing job records",
                            type(error).__name__,
                        )
                        try:
                            await asyncio.wait_for(self.stop.wait(), 2)
                        except TimeoutError:
                            pass
            finally:
                for task in self.active.values():
                    task.cancel()
                await asyncio.gather(*self.active.values(), return_exceptions=True)
                await self.processes.close()
                await asyncio.gather(
                    *(extension.close() for extension in self.extensions.values()),
                    return_exceptions=True,
                )
                try:
                    await self.flush(client)
                except (httpx.HTTPError, ValueError, OSError):
                    logger.info("Unacknowledged results are preserved for the next Runner start")
                self.db.close()
                self.lock_file.close()


async def run_runner(config: RunnerConfig):
    runner = Runner(config)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        loop.add_signal_handler(signal.SIGTERM, runner.stop.set)
    try:
        await runner.run()
    finally:
        if os.name != "nt":
            loop.remove_signal_handler(signal.SIGTERM)
