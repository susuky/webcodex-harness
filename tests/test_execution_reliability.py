from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import webcodex.processes as process_module
from webcodex.catalog import OutcomeUnknown
from webcodex.config import RunnerConfig
from webcodex.filesystem import Workspace, sha256
from webcodex.processes import Processes


@pytest_asyncio.fixture
async def processes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": project},
    )
    manager = Processes(config)
    try:
        yield manager
    finally:
        await manager.close()


async def reap_test_children(manager, children):
    for child in children:
        if child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        if child.stdin is not None:
            child.stdin.close()
        async with asyncio.timeout(3):
            if child.stdout is not None:
                await child.stdout.read()
            await child.wait()
    # The old implementation may install a state without a monitor after a failed startup.
    for process_id, state in list(manager.active.items()):
        if "monitor" not in state:
            manager.active.pop(process_id)
            state["log"].close()
            if state["master"] is not None:
                os.close(state["master"])


@pytest.mark.skipif(os.name == "nt", reason="Real Unix process-group fixture")
async def test_log_failure_does_not_launch_an_untracked_process(processes, monkeypatch):
    children = []
    spawn = asyncio.create_subprocess_exec

    async def record_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    (processes.directory / "blocked.log").mkdir()
    try:
        with pytest.raises(IsADirectoryError):
            await processes.start(
                "blocked",
                "task",
                [sys.executable, "-c", "import time; time.sleep(60)"],
                processes.config.projects["project"],
                1,
            )
        assert children == []
        assert not processes.active
    finally:
        await reap_test_children(processes, children)


@pytest.mark.skipif(os.name == "nt", reason="Real Unix process-group fixture")
async def test_journal_failure_precedes_spawn(processes, monkeypatch):
    class FailingCommit(sqlite3.Connection):
        fail_commit = False

        def commit(self):
            if self.fail_commit:
                self.fail_commit = False
                self.rollback()
                raise sqlite3.OperationalError("injected journal failure")
            return super().commit()

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is None:
                self.commit()
                return False
            return super().__exit__(exc_type, exc, traceback)

    processes.db.close()
    processes.db = sqlite3.connect(processes.directory / "processes.sqlite3", factory=FailingCommit)
    processes.db.row_factory = sqlite3.Row
    processes.db.fail_commit = True
    children = []
    spawn = asyncio.create_subprocess_exec

    async def record_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected journal"):
            await processes.start(
                "journal",
                "task",
                [sys.executable, "-c", "import time; time.sleep(60)"],
                processes.config.projects["project"],
                1,
            )
        assert children == []
        assert not processes.active
    finally:
        await reap_test_children(processes, children)


@pytest.mark.skipif(os.name == "nt", reason="Real Unix process-group fixture")
@pytest.mark.parametrize("tty", [False, True])
async def test_failed_monitor_start_reaps_and_keeps_replay_identity(processes, monkeypatch, tty):
    children, logs, descriptors = [], [], []
    spawn, path_open = asyncio.create_subprocess_exec, Path.open

    async def record_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    def record_open(path, *args, **kwargs):
        stream = path_open(path, *args, **kwargs)
        if path.suffix == ".log":
            logs.append(stream)
        return stream

    def fail_monitor(*args):
        raise RuntimeError("injected monitor failure")

    if tty:
        openpty = process_module.pty.openpty

        def record_pty():
            pair = openpty()
            descriptors.extend(pair)
            return pair

        monkeypatch.setattr(process_module.pty, "openpty", record_pty)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    monkeypatch.setattr(Path, "open", record_open)
    monkeypatch.setattr(processes, "_monitor", fail_monitor)
    try:
        with pytest.raises(OutcomeUnknown, match="execution may have begun"):
            await processes.start(
                "monitor",
                "task",
                [sys.executable, "-c", "import time; time.sleep(60)"],
                processes.config.projects["project"],
                1,
                tty,
            )
        assert len(children) == 1 and children[0].returncode is not None
        assert not processes.active
        assert all(stream.closed for stream in logs)
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert processes.row("monitor", "task")["status"] == "interrupted"
        assert (
            await processes.start(
                "monitor",
                "task",
                [sys.executable, "-c", "raise SystemExit(0)"],
                processes.config.projects["project"],
                1,
            )
            == "monitor"
        )
        assert len(children) == 1
    finally:
        await reap_test_children(processes, children)


@pytest.mark.skipif(os.name == "nt", reason="Real Unix process-group fixture")
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancellation_during_spawn_waits_for_ownership_and_reaps(
    processes, monkeypatch, cancel_count
):
    children = []
    spawned, release = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    starting = asyncio.create_task(
        processes.start(
            "cancel",
            "task",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            processes.config.projects["project"],
            60,
        )
    )
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        for _ in range(cancel_count):
            starting.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 3)
        assert children[0].returncode is not None
        assert not processes.active
        assert processes.row("cancel", "task")["status"] == "interrupted"
        assert (
            await processes.start(
                "cancel",
                "task",
                [sys.executable, "-c", "raise SystemExit(0)"],
                processes.config.projects["project"],
                1,
            )
            == "cancel"
        )
        assert len(children) == 1
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await reap_test_children(processes, children)


@pytest.mark.skipif(os.name == "nt", reason="Real Unix process-group fixture")
async def test_repeated_cancellation_during_startup_reap_finishes_cleanup(processes, monkeypatch):
    children, logs = [], []
    reaping, release = asyncio.Event(), asyncio.Event()
    spawn, path_open = asyncio.create_subprocess_exec, Path.open

    async def record_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        read = child.stdout.read

        async def slow_read(*args):
            reaping.set()
            await release.wait()
            return await read(*args)

        child.stdout.read = slow_read
        return child

    def record_open(path, *args, **kwargs):
        stream = path_open(path, *args, **kwargs)
        if path.suffix == ".log":
            logs.append(stream)
        return stream

    def fail_monitor(*args):
        raise RuntimeError("injected monitor failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    monkeypatch.setattr(Path, "open", record_open)
    monkeypatch.setattr(processes, "_monitor", fail_monitor)
    starting = asyncio.create_task(
        processes.start(
            "reap",
            "task",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            processes.config.projects["project"],
            60,
        )
    )
    try:
        await asyncio.wait_for(reaping.wait(), 3)
        for _ in range(2):
            starting.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 3)
        assert children[0].returncode is not None
        assert logs and all(stream.closed for stream in logs)
        assert not processes.active
        assert processes.row("reap", "task")["status"] == "interrupted"
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await reap_test_children(processes, children)


@pytest.mark.parametrize("failing_close", [None, "job", "gate", "log"])
async def test_failed_windows_initialization_closes_job_and_gate(
    processes, monkeypatch, failing_close
):
    logs = []
    path_open = Path.open

    def record_open(path, *args, **kwargs):
        stream = path_open(path, *args, **kwargs)
        if path.suffix == ".log":
            logs.append(stream)
            if failing_close == "log":
                close = stream.close

                def failed_close():
                    close()
                    raise OSError("injected log close failure")

                stream.close = failed_close
        return stream

    handles = [SimpleNamespace(closed=False), SimpleNamespace(closed=False)]
    for name, handle in zip(["job", "gate"], handles, strict=True):

        def close(handle=handle, name=name):
            handle.closed = True
            if failing_close == name:
                raise OSError("injected handle close failure")

        handle.Close = close
    child = SimpleNamespace(returncode=None, stdin=None, stdout=None)

    async def wait():
        return child.returncode

    async def spawn(*args):
        return child, handles[0], None, handles[1]

    def fail_monitor(*args):
        raise RuntimeError("injected monitor failure")

    child.wait = wait
    monkeypatch.setattr(process_module, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    monkeypatch.setitem(
        sys.modules,
        "webcodex.windows",
        SimpleNamespace(spawn=spawn, terminate=lambda job: setattr(child, "returncode", 1)),
    )
    monkeypatch.setattr(processes, "_monitor", fail_monitor)
    monkeypatch.setattr(Path, "open", record_open)
    try:
        with pytest.raises(OutcomeUnknown):
            await processes.start("windows", "task", ["probe"], Path.cwd(), 1)
        assert child.returncode == 1
        assert all(handle.closed for handle in handles)
        assert logs and all(stream.closed for stream in logs)
        assert not processes.active
        assert processes.row("windows", "task")["status"] == "interrupted"
    finally:
        for state in processes.active.values():
            with contextlib.suppress(OSError):
                state["log"].close()
        processes.active.clear()


def record_output(processes, data, *, status="completed", truncated=False):
    processes.db.execute(
        "INSERT INTO processes(id,task_id,status,command,cwd,created,bytes,truncated) "
        "VALUES ('output','task',?,'[]','.',0,?,?)",
        (status, len(data), int(truncated)),
    )
    processes.db.commit()
    path = processes.directory / "output.log"
    path.write_bytes(data)
    return path


def paged_output(processes, data, limit):
    cursor, parts = 0, []
    while cursor < len(data):
        result = processes.read("output", "task", cursor, limit)
        assert cursor < result["next_cursor"] <= cursor + limit + 3
        parts.append(result["output"])
        cursor = result["next_cursor"]
    assert not result["has_more"]
    assert "".join(parts) == data.decode("utf-8", errors="replace")


async def test_default_output_page_preserves_utf8(processes):
    data = ("a" * 65535 + "繁體中文🙂Z").encode()
    record_output(processes, data)
    paged_output(processes, data, 65536)


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
async def test_small_pages_advance_by_whole_characters(processes, limit):
    data = "繁é🙂a體中文".encode()
    record_output(processes, data)
    paged_output(processes, data, limit)


async def test_running_output_keeps_partial_character_until_complete(processes):
    path = record_output(processes, b"a\xe7", status="running")
    result = processes.read("output", "task")
    assert result["output"] == "a" and result["next_cursor"] == 1
    waiting = processes.read("output", "task", 1, 1)
    assert waiting["output"] == "" and waiting["next_cursor"] == 1
    with path.open("ab") as stream:
        stream.write(b"\xb9\x81")
    complete = processes.read("output", "task", 1, 1)
    assert complete["output"] == "繁" and complete["next_cursor"] == 4
    with path.open("ab") as stream:
        stream.write(b"\xf0\x9f")
    assert processes.read("output", "task", 4)["next_cursor"] == 4
    processes.db.execute("UPDATE processes SET status='completed' WHERE id='output'")
    processes.db.commit()
    final = processes.read("output", "task", 4, 1)
    assert final["output"] == "�" and final["next_cursor"] == 6
    assert not final["has_more"]


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize("limit", [1, 4])
async def test_terminal_invalid_bytes_make_progress(processes, status, limit):
    data = b"a\xff\xe7\xff\xf0\x9f"
    record_output(processes, data, status=status)
    paged_output(processes, data, limit)


async def test_truncated_running_tail_is_final_and_mid_character_cursor_is_supported(processes):
    record_output(processes, b"a\xe7", status="running", truncated=True)
    paged_output(processes, b"a\xe7", 1)
    (processes.directory / "output.log").write_bytes("繁".encode())
    result = processes.read("output", "task", 1, 1)
    assert result["next_cursor"] == 2
    assert result["output"] == "�"


def test_write_compares_the_version_that_was_read(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_bytes(b"original")
    workspace = Workspace(tmp_path)
    original_stat = os.stat
    changed = False

    def replace_before_stat(target, *args, **kwargs):
        nonlocal changed
        if not changed and str(target).endswith("file.txt"):
            changed = True
            path.write_bytes(b"external edit")
        return original_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", replace_before_stat)
    windows = os.name == "nt"
    # A pinned Windows read rejects the injected write before it can change the file.
    with pytest.raises(
        PermissionError if windows else ValueError, match=None if windows else "changed"
    ):
        workspace.write("file.txt", b"our edit", sha256(b"original"))
    assert path.read_bytes() == (b"original" if windows else b"external edit")
    assert not list(tmp_path.glob(".webcodex-write-*"))


@pytest.mark.skipif(os.name == "nt", reason="Windows pinned reads deny concurrent writes")
def test_read_rejects_changes_during_the_same_fd_read(tmp_path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_bytes(b"original")
    workspace = Workspace(tmp_path)
    original_fstat = os.fstat
    changed = False

    def change_after_first_snapshot(fd):
        nonlocal changed
        info = original_fstat(fd)
        if not changed and info.st_ino == path.stat().st_ino:
            changed = True
            path.write_bytes(b"external edit")
        return info

    monkeypatch.setattr(os, "fstat", change_after_first_snapshot)
    with pytest.raises(ValueError, match="changed while reading"):
        workspace.read("file.txt")
    assert path.read_bytes() == b"external edit"


def test_versioned_reads_preserve_create_hash_and_mode_guards(tmp_path):
    workspace = Workspace(tmp_path)
    created = workspace.write("nested/file.txt", b"original", None)
    path = tmp_path / "nested/file.txt"
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(ValueError, match="changed"):
        workspace.write("nested/file.txt", b"wrong hash", None)
    workspace.write("nested/file.txt", b"updated", created["sha256"])
    assert workspace.read("nested/file.txt") == b"updated"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
