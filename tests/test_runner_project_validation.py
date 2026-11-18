from __future__ import annotations

import asyncio
import json
import time

import pytest

from webcodex.config import RunnerConfig, project_binding
from webcodex.runner import Runner


@pytest.fixture
async def runner(tmp_path):
    roots = {name: tmp_path / name for name in ("current", "unrelated")}
    projects = {name: root / name for name, root in roots.items()}
    for path in projects.values():
        path.mkdir(parents=True)
    instance = Runner(
        RunnerConfig(
            server_url="https://runner.test",
            runner_id="local",
            token="r" * 40,
            state_dir=tmp_path / "state",
            projects=projects,
            project_roots=roots,
        )
    )
    try:
        yield instance
    finally:
        await instance.processes.close()
        for extension in instance.extensions.values():
            await extension.close()
        instance.db.close()
        instance.lock_file.close()


def request(runner, tool="list_files", arguments=None):
    return {
        "id": "job",
        "instance": runner.instance,
        "task_id": "task",
        "project": "current",
        "binding": project_binding(runner.config.projects["current"]),
        "tool": tool,
        "arguments": arguments or {},
    }


def result(runner):
    row = runner.db.execute("SELECT status,result FROM deliveries WHERE id='job'").fetchone()
    return row["status"], json.loads(row["result"])


def make_managed(runner):
    with runner.db:
        runner.db.execute(
            "INSERT INTO managed_projects VALUES (?,?,?,?,?)",
            (
                "current",
                "current",
                "current",
                project_binding(runner.config.projects["current"]),
                project_binding(runner.config.project_roots["current"]),
            ),
        )


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("unrelated_failure", ["slow", "removed"])
async def test_current_project_never_checks_unrelated_paths(
    runner, monkeypatch, managed, unrelated_failure, record_property
):
    if managed:
        make_managed(runner)
    job = request(runner)
    unrelated = runner.config.project_roots["unrelated"]
    if unrelated_failure == "removed":
        runner.config.projects["unrelated"].rmdir()
        unrelated.rmdir()
    calls = []

    def inspect(path, **options):
        if path == unrelated or path.is_relative_to(unrelated):
            calls.append(path)
            if unrelated_failure == "slow":
                time.sleep(0.15)
        return project_binding(path, **options)

    monkeypatch.setattr("webcodex.runner.project_binding", inspect)
    monkeypatch.setattr("webcodex.projects.project_binding", inspect)

    async def pulse():
        started = time.monotonic()
        await asyncio.sleep(0.01)
        return (time.monotonic() - started) * 1000

    heartbeat = asyncio.create_task(pulse())
    await asyncio.sleep(0)
    await runner.execute(job)
    record_property("ten_ms_pulse_ms", round(await heartbeat, 2))
    assert result(runner)[0] == "succeeded"
    assert calls == []


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("change", ["removed", "replaced"])
async def test_current_project_still_requires_its_original_binding(runner, managed, change):
    if managed:
        make_managed(runner)
    job = request(runner)
    root = runner.config.projects["current"]
    if change == "removed":
        root.rmdir()
    else:
        root.rename(root.with_name("original"))
        root.mkdir()
    await runner.execute(job)
    assert result(runner)[0] == "failed"


@pytest.mark.parametrize("change", ["parent_replaced", "parent_unregistered", "record_changed"])
async def test_managed_project_keeps_registered_target_and_parent_bindings(runner, change):
    make_managed(runner)
    job = request(runner)
    parent = runner.config.project_roots["current"]
    if change == "parent_replaced":
        saved = parent.with_name("original-parent")
        parent.rename(saved)
        parent.mkdir()
        (saved / "current").rename(parent / "current")
        assert project_binding(runner.config.projects["current"]) == job["binding"]
    elif change == "parent_unregistered":
        del runner.config.project_roots["current"]
    else:
        with runner.db:
            runner.db.execute("UPDATE managed_projects SET binding=?", ("0" * 64,))
    await runner.execute(job)
    assert result(runner)[0] == "failed"


def skill(runner, content):
    bundle = runner.config.projects["current"] / ".agents/skills/probe"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: probe\ndescription: Test script\n---\n", encoding="utf-8"
    )
    (bundle / "script.py").write_bytes(content)
    return request(runner, "skill_run", {"name": "project:probe", "script": "script.py"})


@pytest.mark.parametrize("worker", [False, True])
async def test_skill_script_uses_process_worker_check_once(runner, monkeypatch, worker):
    from webcodex import execution_policy

    content = (
        b"import subprocess\nsubprocess.run(['codex', 'exec', 'test'])\n"
        if worker
        else b"print('ordinary-skill-script')\n"
    )
    job = skill(runner, content)
    parses = []
    parse = execution_policy._parse_python

    def observed(source, **options):
        parses.append(source)
        return parse(source, **options)

    monkeypatch.setattr(execution_policy, "_parse_python", observed)
    await runner.execute(job)
    status, payload = result(runner)
    assert parses == [content.decode("utf-8")]
    if worker:
        assert status == "failed" and "External Codex/Work workers are disabled" in payload["error"]
        assert runner.processes.db.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    else:
        assert status == "succeeded"
        await runner.processes.wait("job", "task", 3)
        outcome = runner.processes.read("job", "task")
        assert outcome["status"] == "completed" and outcome["exit_code"] == 0
        assert "ordinary-skill-script" in outcome["output"]


@pytest.mark.parametrize(
    "content",
    [b"#" * (2 * 1024 * 1024 + 1), b"#\xff\nprint('not-utf8')\n"],
    ids=["oversized", "non-utf8"],
)
async def test_skill_script_read_constraints_still_precede_execution(runner, content):
    await runner.execute(skill(runner, content))
    assert result(runner)[0] == "failed"
    assert runner.processes.db.execute("SELECT count(*) FROM processes").fetchone()[0] == 0


async def test_registration_checks_each_directory_once_outside_event_loop(runner, monkeypatch):
    import threading

    import httpx

    calls = []
    loop_thread = threading.get_ident()
    received = {}

    def inspect(path, **options):
        assert threading.get_ident() != loop_thread
        calls.append(path)
        return project_binding(path, **options)

    async def receive(request):
        received.update(json.loads(request.content))
        return httpx.Response(200, json={"registered": True})

    monkeypatch.setattr("webcodex.projects.project_binding", inspect)
    async with httpx.AsyncClient(
        base_url="https://runner.test", transport=httpx.MockTransport(receive)
    ) as client:
        await runner.register(client)
    expected_paths = [*runner.config.projects.values(), *runner.config.project_roots.values()]
    assert sorted(calls) == sorted(expected_paths)
    assert set(received["projects"]) == set(runner.config.projects)
    assert received["project_roots"]["current"]["projects"] == ["current"]
    assert received["project_roots"]["unrelated"]["projects"] == ["unrelated"]
