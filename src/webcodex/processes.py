from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import signal
import sqlite3
import struct
import time
from pathlib import Path

from .config import RunnerConfig, private_dir

if os.name != "nt":
    import fcntl
    import pty
    import termios


def environment(config: RunnerConfig) -> dict[str, str]:
    names = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL"}
    if os.name == "nt":
        names |= {
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMDATA",  # MSVC discovery needs the Visual Studio installation metadata.
        }
    result = {
        k.upper() if os.name == "nt" else k: v
        for k, v in os.environ.items()
        if (k.upper() if os.name == "nt" else k) in names
    }
    result.update({"TERM": "xterm-256color", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    result.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    result.update({k.upper() if os.name == "nt" else k: v for k, v in config.environment.items()})
    return result


class Processes:
    def __init__(self, config: RunnerConfig):
        self.config = config
        self.directory = private_dir(config.state_dir / "processes")
        self.db = sqlite3.connect(self.directory / "processes.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS processes (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
            command TEXT NOT NULL, cwd TEXT NOT NULL, exit_code INTEGER,
            created REAL NOT NULL, finished REAL, bytes INTEGER NOT NULL DEFAULT 0,
            truncated INTEGER NOT NULL DEFAULT 0
        )""")
        self.db.execute(
            "UPDATE processes SET status='interrupted',finished=? WHERE status='running'",
            (time.time(),),
        )
        self.db.commit()
        self.active: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    def row(self, process_id: str, task_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM processes WHERE id=? AND task_id=?", (process_id, task_id)
        ).fetchone()
        if row is None:
            raise ValueError("Process not found in this task")
        return dict(row)

    async def start(
        self,
        process_id: str,
        task_id: str,
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
        tty: bool = False,
    ) -> str:
        async with self.lock:
            if self.db.execute("SELECT 1 FROM processes WHERE id=?", (process_id,)).fetchone():
                self.row(process_id, task_id)
                return process_id
            if len(self.active) >= self.config.max_processes:
                raise ValueError("Too many running processes; cancel or wait for one first")
            if not argv or not argv[0] or any("\x00" in arg for arg in argv):
                raise ValueError("A nonempty command without NUL characters is required")
            master = slave = job = terminal = gate = None
            if tty and os.name != "nt":
                master, slave = pty.openpty()
                os.set_blocking(master, False)
            try:
                if os.name == "nt":
                    from .windows import spawn

                    proc, job, terminal, gate = await spawn(
                        argv, cwd, environment(self.config), tty
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=cwd,
                        env=environment(self.config),
                        start_new_session=True,
                        stdin=slave if tty else asyncio.subprocess.PIPE,
                        stdout=slave if tty else asyncio.subprocess.PIPE,
                        stderr=slave if tty else asyncio.subprocess.STDOUT,
                    )
            except BaseException:
                if master is not None:
                    os.close(master)
                raise
            finally:
                if slave is not None:
                    os.close(slave)
            self.db.execute(
                "INSERT INTO processes(id,task_id,status,command,cwd,created) VALUES (?,?,'running',?,?,?)",
                (process_id, task_id, json.dumps(argv), str(cwd), time.time()),
            )
            self.db.commit()
            log = (self.directory / (process_id + ".log")).open("wb")
            state = {
                "proc": proc,
                "master": master,
                "terminal": terminal,
                "job": job,
                "gate": gate,
                "log": log,
                "bytes": 0,
                "stored": 0,
                "truncated": False,
                "cancelled": False,
            }
            self.active[process_id] = state
            state["monitor"] = asyncio.create_task(self._monitor(process_id, timeout_seconds))
            return process_id

    def _append(self, state: dict, chunk: bytes):
        state["bytes"] += len(chunk)
        room = max(0, self.config.max_output_bytes - state["stored"])
        state["log"].write(chunk[:room])
        state["log"].flush()
        state["stored"] += min(len(chunk), room)
        state["truncated"] = state["bytes"] > state["stored"]

    @staticmethod
    def _signal(state: dict, sig: int):
        if state["job"] is not None:
            from .windows import terminate

            terminate(state["job"])
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(state["proc"].pid, sig)

    async def _monitor(self, process_id: str, timeout_seconds: int):
        state = self.active[process_id]
        proc, master = state["proc"], state["master"]
        loop = asyncio.get_running_loop()
        pump = None

        def read_pty():
            try:
                while chunk := os.read(master, 65536):
                    self._append(state, chunk)
                loop.remove_reader(master)
            except BlockingIOError:
                pass
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                loop.remove_reader(master)

        async def read_pipe():
            while chunk := await proc.stdout.read(65536):
                self._append(state, chunk)

        async def read_console():
            terminal = state["terminal"]
            while True:
                try:
                    chunk = terminal.read(blocking=False)
                except Exception:
                    if proc.returncode is None:
                        raise
                    break
                if chunk:
                    self._append(state, chunk.encode("utf-8"))
                elif proc.returncode is not None:
                    break
                await asyncio.sleep(0.02)

        if master is not None:
            loop.add_reader(master, read_pty)
        elif state["terminal"] is not None:
            pump = asyncio.create_task(read_console())
        else:
            pump = asyncio.create_task(read_pipe())
        status = "completed"
        try:
            async with asyncio.timeout(timeout_seconds):
                # Process.wait can wait for inherited pipes after the leader exits.
                while proc.returncode is None:  # noqa: ASYNC110 — leader exit precedes inherited pipe EOF
                    if pump is not None and pump.done():
                        pump.result()
                    await asyncio.sleep(0.05)
        except TimeoutError:
            status = "timed_out"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as error:
            status = "failed"
            self._append(state, f"\nOutput capture failed: {type(error).__name__}\n".encode())
        finally:
            self._signal(state, getattr(signal, "SIGKILL", signal.SIGTERM))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
            if pump is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(pump, 2)
            else:
                read_pty()
                loop.remove_reader(master)
                os.close(master)
            state["log"].close()
            if state["job"] is not None:
                state["job"].Close()
                state["gate"].Close()
            if state["cancelled"]:
                status = "cancelled"
            elif status == "completed" and proc.returncode != 0:
                status = "failed"
            self.db.execute(
                "UPDATE processes SET status=?,exit_code=?,finished=?,bytes=?,truncated=? WHERE id=?",
                (
                    status,
                    proc.returncode,
                    time.time(),
                    state["bytes"],
                    int(state["truncated"]),
                    process_id,
                ),
            )
            self.db.commit()
            del self.active[process_id]

    def read(self, process_id: str, task_id: str, cursor: int = 0, limit: int = 65536) -> dict:
        row = self.row(process_id, task_id)
        log = self.directory / (process_id + ".log")
        size = log.stat().st_size if log.exists() else 0
        if cursor > size:
            raise ValueError("Cursor exceeds available output")
        output = b""
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(cursor)
                output = stream.read(limit)
        state = self.active.get(process_id)
        return {
            "process_id": process_id,
            "status": row["status"],
            "exit_code": row["exit_code"],
            "output": output.decode("utf-8", errors="replace"),
            "cursor": cursor,
            "next_cursor": cursor + len(output),
            "has_more": cursor + len(output) < size,
            "truncated": bool(state["truncated"] if state else row["truncated"]),
            "total_bytes": state["bytes"] if state else row["bytes"],
        }

    async def wait(self, process_id: str, task_id: str, seconds: float):
        self.row(process_id, task_id)
        state = self.active.get(process_id)
        if state:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(state["monitor"]), seconds)

    async def write(self, process_id: str, task_id: str, chars: str, eof: bool):
        self.row(process_id, task_id)
        state = self.active.get(process_id)
        if not state:
            raise ValueError("Process is no longer running")
        if state["terminal"] is not None:
            data = chars.replace("\r\n", "\n").replace("\n", "\r") + ("\x1a\r" if eof else "")
            state["terminal"].write(data)
        elif state["master"] is not None:
            data = chars.encode() + (b"\x04" if eof else b"")
            if chars == "\x03":
                self._signal(state, signal.SIGINT)
                return
            # Small, bounded writes avoid blocking the Runner on a full terminal.
            sent = os.write(state["master"], data)
            if sent != len(data):
                raise ValueError(
                    f"Terminal accepted only {sent} bytes; do not repeat the full input"
                )
        else:
            state["proc"].stdin.write(chars.encode())
            await asyncio.wait_for(state["proc"].stdin.drain(), 3)
            if eof:
                state["proc"].stdin.close()

    def resize(self, process_id: str, task_id: str, rows: int, columns: int):
        self.row(process_id, task_id)
        state = self.active.get(process_id)
        if state and state["terminal"] is not None:
            state["terminal"].set_size(columns, rows)
            return
        if not state or state["master"] is None:
            raise ValueError("No active terminal for this process")
        fcntl.ioctl(state["master"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self._signal(state, signal.SIGWINCH)

    async def cancel(self, process_id: str, task_id: str):
        self.row(process_id, task_id)
        state = self.active.get(process_id)
        if state:
            state["cancelled"] = True
            self._signal(state, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(state["monitor"]), 1)
            except TimeoutError:
                self._signal(state, getattr(signal, "SIGKILL", signal.SIGTERM))
                await state["monitor"]

    def list(self, task_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id AS process_id,status,exit_code,created,finished FROM processes "
                "WHERE task_id=? ORDER BY created DESC LIMIT 100",
                (task_id,),
            )
        ]

    async def close(self):
        states = list(self.active.values())
        for state in states:
            state["cancelled"] = True
            self._signal(state, getattr(signal, "SIGKILL", signal.SIGTERM))
        await asyncio.gather(*(state["monitor"] for state in states), return_exceptions=True)
        self.db.close()
