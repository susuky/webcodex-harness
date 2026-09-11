from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3

import httpx

from .catalog import OutcomeUnknown, definitions
from .config import RunnerConfig, identifier, private_dir
from .extensions import Extensions
from .filesystem import Workspace
from .processes import Processes
from .runtime import Runtime
from .store import new_id

logger = logging.getLogger(__name__)
DEFINITIONS = definitions(Runtime, remote=True)


class Runner:
    def __init__(self, config: RunnerConfig):
        self.config = config
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
        self.processes = Processes(config)
        self.extensions = {project: Extensions(config, project) for project in config.projects}
        self.locks = {project: asyncio.Lock() for project in config.projects}
        self.active: dict[str, asyncio.Task] = {}
        self.stop = asyncio.Event()
        self.delivery_lock = asyncio.Lock()

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
            definition = DEFINITIONS.get(name)
            if not definition or definition.permission not in self.config.permissions:
                raise ValueError("Tool is not permitted by this Runner")
            if self.config.inventory().get(project) != job["binding"]:
                raise ValueError("Project binding does not match this Runner")
            args = definition.model.model_validate(
                {**job["arguments"], "task_id": job["task_id"]}
            ).model_dump()
            args.pop("task_id")
            args.pop("request_id")
            runtime = Runtime(
                self.config,
                Workspace(self.config.projects[project]),
                self.processes,
                self.extensions[project],
                job["task_id"],
                job_id,
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

    async def flush(self, client: httpx.AsyncClient):
        async with self.delivery_lock:
            await self._flush(client)

    async def _flush(self, client: httpx.AsyncClient):
        for row in list(
            self.db.execute(
                "SELECT * FROM deliveries WHERE acknowledged=0 AND status!='running' LIMIT 100"
            )
        ):
            response = await client.post(
                "/api/runner/complete",
                json={
                    "job_id": row["id"],
                    "instance": row["instance"],
                    "status": row["status"],
                    "result": json.loads(row["result"]),
                },
            )
            response.raise_for_status()
            self.db.execute("UPDATE deliveries SET acknowledged=1 WHERE id=?", (row["id"],))
            self.db.commit()

    async def execute_and_deliver(self, job: dict, client: httpx.AsyncClient):
        await self.execute(job)
        try:
            await self.flush(client)
        except httpx.HTTPError:
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
                            known = [
                                r[0]
                                for r in self.db.execute(
                                    "SELECT id FROM deliveries WHERE acknowledged=0 LIMIT 10000"
                                )
                            ]
                            response = await client.post(
                                "/api/runner/register",
                                json={
                                    "instance": self.instance,
                                    "projects": self.config.inventory(),
                                    "known_jobs": known,
                                },
                            )
                            response.raise_for_status()
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
                    except (httpx.HTTPError, ValueError, KeyError) as error:
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
                except httpx.HTTPError:
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
