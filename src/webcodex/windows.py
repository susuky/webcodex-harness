"""Native Windows handles, permissions and process containment."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32job
import win32security

BOOTSTRAP = """
import contextlib, json, subprocess, sys
import win32con, win32event
with contextlib.closing(win32event.OpenEvent(win32con.SYNCHRONIZE, False, sys.argv[1])) as event:
    if win32event.WaitForSingleObject(event, 10000) != win32event.WAIT_OBJECT_0:
        raise SystemExit('Runner did not authorize process startup')
try:
    raise SystemExit(subprocess.call(json.loads(sys.argv[2])))
except KeyboardInterrupt:
    raise SystemExit(130) from None
"""


def private_acl(path: Path):
    with contextlib.closing(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    acl = win32security.ACL()
    inherit = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    for sid in [user, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)]:
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


@contextlib.contextmanager
def pinned(path: Path, *, directory: bool):
    """Deny rename/delete while open; leaf reads also deny concurrent writes."""
    try:
        handle = win32file.CreateFile(
            str(path),
            ntsecuritycon.FILE_READ_ATTRIBUTES if directory else win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | (win32con.FILE_SHARE_WRITE if directory else 0),
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        with contextlib.closing(handle):
            info = win32file.GetFileInformationByHandle(handle)
            if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Symlinks and reparse points are not allowed")
            if bool(info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory:
                raise ValueError(
                    "Expected a directory" if directory else "Only regular files can be read"
                )
            if not directory and (
                info[7] != 1 or win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK
            ):
                raise ValueError("Only regular, single-link files can be read")
            # Reject DOS short-name aliases of private metadata as well as literal names.
            if Path(win32file.GetFinalPathNameByHandle(handle, 0)).name.casefold() in {
                ".git",
                ".webcodex",
            }:
                raise ValueError("Path targets private metadata")
            yield handle
    except pywintypes.error as error:
        raise OSError(0, error.strerror, str(path), error.winerror) from None


class TerminalProcess:
    def __init__(self, terminal):
        self.terminal = terminal
        self.pid = terminal.pid

    @property
    def returncode(self):
        return self.terminal.get_exitstatus()

    async def wait(self):
        while self.returncode is None:  # noqa: ASYNC110 — ConPTY exposes polling, not an awaitable
            await asyncio.sleep(0.02)
        return self.returncode


async def spawn(argv: list[str], cwd: Path, env: dict[str, str], tty: bool):
    """The trusted bootstrap cannot execute argv until assigned to the Job Object."""
    job = win32job.CreateJobObject(None, "")
    event = proc = None
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        name = "Local\\webcodex-" + uuid.uuid4().hex
        event = win32event.CreateEvent(None, True, False, name)
        bootstrap = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-c",
            BOOTSTRAP,
            name,
            json.dumps(argv),
        ]
        terminal = None
        if tty:
            from winpty import PTY, Backend

            terminal = PTY(80, 24, backend=Backend.ConPTY)
            terminal.spawn(
                bootstrap[0],
                cwd=str(cwd),
                env="\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0",
                cmdline=" " + subprocess.list2cmdline(bootstrap[1:]),
            )
            proc = TerminalProcess(terminal)
        else:
            proc = await asyncio.create_subprocess_exec(
                *bootstrap,
                cwd=cwd,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        with contextlib.closing(
            win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid
            )
        ) as handle:
            win32job.AssignProcessToJobObject(job, handle)
        win32event.SetEvent(event)
        result = proc, job, terminal, event
        event = None  # Keep the named gate alive until the process is reaped.
        return result
    except BaseException:
        if proc is not None:
            # Assignment itself may have failed; stop the still-gated bootstrap too.
            with contextlib.suppress(OSError, pywintypes.error):
                with contextlib.closing(
                    win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, proc.pid)
                ) as handle:
                    win32api.TerminateProcess(handle, 1)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
        job.Close()
        raise
    finally:
        if event is not None:
            event.Close()


def terminate(job):
    win32job.TerminateJobObject(job, 1)
