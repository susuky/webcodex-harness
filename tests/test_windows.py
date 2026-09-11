"""These tests require real Windows APIs; Linux skips are not Windows evidence."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

import pytest

from webcodex.config import RunnerConfig, initialize, load_config
from webcodex.filesystem import Workspace
from webcodex.processes import Processes

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows")


def test_windows_paths_acl_and_utf8_configuration(tmp_path):
    import win32api
    import win32con
    import win32security

    root = tmp_path / "中文 repo"
    root.mkdir()
    directory = tmp_path / "設定"
    initialize(directory, root, 8765, False)
    config = load_config(directory / "runner.json", RunnerConfig)
    assert config.projects["project"] == root.resolve()
    descriptor = win32security.GetNamedSecurityInfo(
        str(directory / "runner.json"),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    with contextlib.closing(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    ) as token:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    allowed = {str(user), str(win32security.CreateWellKnownSid(win32security.WinLocalSystemSid))}
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 2
    assert {str(acl.GetAce(i)[2]) for i in range(acl.GetAceCount())} == allowed
    workspace = Workspace(root)
    for path in [
        "C:/outside",
        "file:stream",
        "NUL",
        "COM1.txt",
        "file.",
        "file ",
        ".GIT/config",
        ".WEBCODEX/key",
        "CONIN$",
        "bad?name",
    ]:
        with pytest.raises(ValueError):
            workspace.write(path, b"blocked", None)
    record = workspace.write("資料/說明.txt", "繁體中文\n".encode(), None)
    assert workspace.read("資料/說明.txt").decode() == "繁體中文\n"
    workspace.delete("資料/說明.txt", record["sha256"])
    assert not (root / "資料/說明.txt").exists()


async def test_windows_job_assignment_failure_never_executes_command(tmp_path, monkeypatch):
    import win32job

    from webcodex.windows import spawn

    def reject(*args):
        raise RuntimeError("Job assignment denied")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", reject)
    with pytest.raises(RuntimeError, match="denied"):
        await spawn(
            [sys.executable, "-c", "from pathlib import Path; Path('escaped').touch()"],
            tmp_path,
            dict(os.environ),
            False,
        )
    await asyncio.sleep(0.2)
    assert not (tmp_path / "escaped").exists()


async def test_windows_cancel_reaps_grandchildren(tmp_path):
    import win32api
    import win32con
    import win32event

    root = tmp_path / "project"
    root.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": root},
    )
    processes = Processes(config)
    code = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    try:
        await processes.start("tree", "task", [sys.executable, "-c", code], root, 60)
        deadline = time.monotonic() + 10
        while not (root / "child.pid").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        child = int((root / "child.pid").read_text(encoding="utf-8"))
        with contextlib.closing(win32api.OpenProcess(win32con.SYNCHRONIZE, False, child)) as handle:
            await processes.cancel("tree", "task")
            assert win32event.WaitForSingleObject(handle, 3000) == win32event.WAIT_OBJECT_0
        assert processes.read("tree", "task")["status"] == "cancelled"
    finally:
        await processes.close()


async def test_windows_runner_crash_closes_job(tmp_path):
    import win32api
    import win32con
    import win32event

    root = tmp_path / "project"
    root.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": root},
    )
    child_code = "import os,time; from pathlib import Path; Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"
    script = tmp_path / "driver.py"
    script.write_text(
        "import asyncio,sys\nfrom webcodex.config import RunnerConfig\nfrom webcodex.processes import Processes\n"
        f"config=RunnerConfig.model_validate_json({config.model_dump_json()!r})\n"
        "async def main():\n    manager=Processes(config)\n"
        f"    await manager.start('child','task',[sys.executable,'-c',{child_code!r}],config.projects['project'],60)\n"
        "    await asyncio.sleep(60)\nasyncio.run(main())\n",
        encoding="utf-8",
        newline="\n",
    )
    driver = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "child.pid").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        child = int((root / "child.pid").read_text(encoding="utf-8"))
        with contextlib.closing(win32api.OpenProcess(win32con.SYNCHRONIZE, False, child)) as handle:
            driver.kill()
            await asyncio.wait_for(driver.wait(), 5)
            assert win32event.WaitForSingleObject(handle, 3000) == win32event.WAIT_OBJECT_0
    finally:
        if driver.returncode is None:
            driver.kill()
        await asyncio.wait_for(driver.communicate(), 5)
