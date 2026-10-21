from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .config import RunnerConfig, identifier
from .filesystem import Workspace
from .processes import Processes, environment, executable

MAX_EVENT_BYTES = 256 * 1024
MAX_PARSE_BYTES = 8 * 1024 * 1024
MAX_FINAL_CHARS = 64000


def summary(result: dict, limit: int) -> dict:
    """Keep aggregate tool results below the Runner transport limit."""
    return {
        **result,
        "final": result["final"][:limit],
        "final_truncated": len(result["final"]) > limit,
        "error": result["error"][:1000] if result["error"] else None,
    }


class Subagents:
    def __init__(self, config: RunnerConfig, processes: Processes):
        self.config, self.processes = config, processes
        self.db = processes.db
        self.lock = asyncio.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, name TEXT NOT NULL,
            request_hash TEXT NOT NULL, writable INTEGER NOT NULL,
            created REAL NOT NULL, status TEXT NOT NULL, result TEXT
        )""")
        # A committed launch identity without a process is uncertain after a restart.
        self.db.execute(
            "UPDATE subagents SET status='interrupted' WHERE status='running' "
            "AND NOT EXISTS (SELECT 1 FROM processes WHERE processes.id=subagents.id)"
        )
        self.db.commit()

    def _row(self, agent_id: str, task_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM subagents WHERE id=? AND task_id=?", (agent_id, task_id)
        ).fetchone()
        if row is None:
            raise ValueError("Subagent not found in this task")
        return dict(row)

    def _command(self, workspace: Workspace, write: bool) -> tuple[list[str], dict]:
        inherited = environment(self.config)
        if any(
            name.upper().startswith(("OPENAI_", "CODEX_")) and name.upper() != "CODEX_HOME"
            for name in self.config.environment
        ):
            raise ValueError("Subagents require Codex login without provider or internal overrides")
        home = (
            Path(
                inherited.get("CODEX_HOME")
                or os.environ.get("CODEX_HOME")
                or Path.home() / ".codex"
            )
            .expanduser()
            .resolve()
        )
        if not (home / "auth.json").is_file():
            raise ValueError("Sign in with the Codex CLI before starting a subagent")
        # Ignore user settings and mark every possible repository ancestor untrusted.
        # Share auth in place: copying refresh tokens can invalidate the original login.
        projects = ",".join(
            json.dumps(str(path), ensure_ascii=False) + '={trust_level="untrusted"}'
            for path in (workspace.root, *workspace.root.parents)
        )
        argv = [
            executable("codex", self.config),
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write" if write else "read-only",
            "--color",
            "never",
            "-c",
            'approval_policy="never"',
            "-c",
            'model_provider="openai"',
            "-c",
            "agents.enabled=false",
            "-c",
            "projects={" + projects + "}",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
        ]
        for feature in (
            "hooks",
            "plugins",
            "remote_plugin",
            "apps",
            "multi_agent",
            "multi_agent_v2",
        ):
            argv.extend(["--disable", feature])
        argv.append("-")
        return argv, {"CODEX_HOME": str(home)}

    async def spawn(
        self,
        agent_id: str,
        task_id: str,
        workspace: Workspace,
        prompt: str,
        name: str,
        write: bool = False,
        timeout_seconds: int = 1800,
    ) -> dict:
        if not self.config.subagents_enabled:
            raise ValueError("Subagents are disabled in this Runner's configuration")
        identifier(agent_id)
        identifier(task_id)
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > 64000
            or "\x00" in prompt
        ):
            raise ValueError("Subagent prompt must contain 1–64,000 characters without NUL")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or "\x00" in name:
            raise ValueError("Subagent name must contain 1–200 characters without NUL")
        if not isinstance(write, bool):
            raise ValueError("Subagent write must be a boolean")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= self.config.max_process_seconds
        ):
            raise ValueError("Subagent timeout exceeds this Runner's process timeout")
        request_hash = hashlib.sha256(
            json.dumps([str(workspace.root), prompt, name, write, timeout_seconds]).encode()
        ).hexdigest()
        async with self.lock:
            existing = self.db.execute("SELECT * FROM subagents WHERE id=?", (agent_id,)).fetchone()
            if existing is not None:
                self._row(agent_id, task_id)
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "Subagent launch identity already belongs to a different request"
                    )
                return self.get(agent_id, task_id)
            if self.db.execute("SELECT 1 FROM processes WHERE id=?", (agent_id,)).fetchone():
                raise ValueError("Subagent launch identity already belongs to another process")
            active = self.db.execute(
                "SELECT COUNT(*) FROM subagents s LEFT JOIN processes p ON p.id=s.id "
                "WHERE s.status='running' AND (p.status='running' OR p.id IS NULL)"
            ).fetchone()[0]
            if active >= self.config.max_subagents:
                raise ValueError("Too many running subagents; cancel or wait for one first")
            with self.db:
                self.db.execute(
                    "INSERT INTO subagents VALUES (?,?,?,?,?,?,'running',NULL)",
                    (agent_id, task_id, name, request_hash, int(write), time.time()),
                )
            try:
                argv, env = self._command(workspace, write)
                with workspace.directory() as directory:
                    cwd = workspace.root
                    if sys.platform == "linux":
                        cwd = Path(f"/proc/{os.getpid()}/fd/{directory}")
                    await self.processes.start(
                        agent_id, task_id, argv, cwd, timeout_seconds, env=env
                    )
                await self.processes.write(agent_id, task_id, prompt, True)
            except BaseException:
                # Once journalled, failures and cancellation must never launch a replacement.
                if agent_id in self.processes.active:
                    await self.processes.cancel(agent_id, task_id)
                with self.db:
                    self.db.execute("UPDATE subagents SET status='failed' WHERE id=?", (agent_id,))
                raise
        return self.get(agent_id, task_id)

    def _events(self, agent_id: str, *, terminal: bool = False) -> dict:
        final, error, completed = "", None, False
        usage = None
        limited = False
        log = self.processes.directory / (agent_id + ".log")
        budget = min(self.config.max_output_bytes, MAX_PARSE_BYTES)
        if not log.exists():
            return {
                "final": final,
                "error": error,
                "turn_completed": completed,
                "usage": usage,
                "limited": limited,
            }
        with log.open("rb") as stream:
            while budget > 0:
                line = stream.readline(min(MAX_EVENT_BYTES + 1, budget))
                if not line:
                    break
                budget -= len(line)
                if len(line) > MAX_EVENT_BYTES:
                    limited = True
                    while line and not line.endswith(b"\n") and budget > 0:
                        line = stream.readline(min(65536, budget))
                        budget -= len(line)
                    continue
                if not line.endswith(b"\n") and not terminal:
                    break  # A running process may still be writing this event.
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue  # stderr shares the durable process log.
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                item = event.get("item")
                if kind == "item.completed" and isinstance(item, dict):
                    if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                        text = item["text"]
                        final = text[:MAX_FINAL_CHARS]
                        limited |= len(text) > MAX_FINAL_CHARS
                elif kind == "turn.completed":
                    completed = True
                    raw_usage = event.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = {
                            k: v
                            for k, v in raw_usage.items()
                            if k in {"input_tokens", "cached_input_tokens", "output_tokens"}
                            and isinstance(v, int)
                            and not isinstance(v, bool)
                            and 0 <= v < 2**63
                        }
                elif kind in {"turn.failed", "error"}:
                    value = event.get("error", event.get("message", "Codex reported an error"))
                    if isinstance(value, dict):
                        value = value.get("message", "Codex reported an error")
                    error = str(value)[:4000]
            limited |= stream.read(1) != b""
        return {
            "final": final,
            "error": error,
            "turn_completed": completed,
            "usage": usage,
            "limited": limited,
        }

    def get(self, agent_id: str, task_id: str) -> dict:
        row = self._row(agent_id, task_id)
        if row["result"] is not None:
            return json.loads(row["result"])
        process = self.db.execute(
            "SELECT * FROM processes WHERE id=? AND task_id=?", (agent_id, task_id)
        ).fetchone()
        status = process["status"] if process is not None else row["status"]
        events = self._events(agent_id, terminal=status != "running")
        truncated = events["limited"] or bool(process and process["truncated"])
        error = events["error"]
        if status == "completed":
            if (
                error
                or truncated
                or process["exit_code"] != 0
                or not events["turn_completed"]
                or not events["final"].strip()
            ):
                status = "failed"
                error = error or (
                    "Subagent output exceeded the capture limit"
                    if truncated
                    else "Codex exited without a completed assistant response"
                )
        elif status == "failed" and not error:
            error = "Codex process failed; inspect the process output"
        result = {
            "agent_id": agent_id,
            "task_id": task_id,
            "name": row["name"],
            "status": status,
            "write": bool(row["writable"]),
            "created": row["created"],
            "finished": process["finished"] if process else None,
            "process_id": agent_id,
            "exit_code": process["exit_code"] if process else None,
            "final": events["final"],
            "error": error,
            "usage": events["usage"],
            "output_truncated": truncated,
        }
        if status != "running":
            with self.db:
                self.db.execute(
                    "UPDATE subagents SET status=?,result=? WHERE id=?",
                    (status, json.dumps(result, ensure_ascii=False), agent_id),
                )
        return result

    def list(self, task_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id FROM subagents WHERE task_id=? ORDER BY created DESC LIMIT 100", (task_id,)
        ).fetchall()
        return [summary(self.get(row["id"], task_id), 2000) for row in rows]

    async def wait(self, agent_ids: list[str], task_id: str, wait_seconds: float = 30) -> dict:
        if not agent_ids or len(agent_ids) > 16:
            raise ValueError("Wait requires 1–16 subagent IDs")
        if not 0 <= wait_seconds <= 60:
            raise ValueError("Subagent wait must be between 0 and 60 seconds")
        results = [self.get(agent_id, task_id) for agent_id in agent_ids]
        if all(result["status"] == "running" for result in results) and wait_seconds:
            monitors = {
                self.processes.active[agent_id]["monitor"]
                for agent_id in agent_ids
                if agent_id in self.processes.active
            }
            if monitors:
                await asyncio.wait(
                    monitors, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED
                )
            results = [self.get(agent_id, task_id) for agent_id in agent_ids]
        return {
            "agents": [summary(result, 16000) for result in results],
            "timed_out": all(r["status"] == "running" for r in results),
        }

    async def cancel(self, agent_id: str, task_id: str) -> dict:
        async with self.lock:
            result = self.get(agent_id, task_id)
            if result["status"] == "running":
                await self.processes.cancel(agent_id, task_id)
                result = self.get(agent_id, task_id)
            return result
