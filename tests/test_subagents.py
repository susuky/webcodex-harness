from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import pytest
import pytest_asyncio

import webcodex.subagents as subagent_module
from webcodex.config import RunnerConfig
from webcodex.filesystem import Workspace
from webcodex.processes import Processes
from webcodex.subagents import Subagents

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Executable Unix fake Codex CLI")


def launch_count(binary):
    path = Path(str(binary) + ".launches")
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest_asyncio.fixture
async def agents(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\n"
        "request=json.loads(sys.stdin.read())\n"
        "with open(__file__+'.launches','a') as stream: stream.write('started\\n')\n"
        "time.sleep(request.get('delay',0))\n"
        "def emit(value): print(json.dumps(value),flush=True)\n"
        "print('diagnostic on stderr',file=sys.stderr,flush=True)\n"
        f"if request.get('oversized'): emit({{'type':'ignored','text':'x'*{subagent_module.MAX_EVENT_BYTES + 1}}})\n"
        "if request.get('error'): emit({'type':'turn.failed','error':{'message':'quota reached'}})\n"
        "if 'final' in request:\n"
        " emit({'type':'item.completed','item':{'type':'agent_message','text':request['final']}})\n"
        "if request.get('inspect'):\n"
        " emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'codex_home':os.environ.get('CODEX_HOME')})}})\n"
        "if not request.get('no_turn'): emit({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}})\n"
        "sys.exit(request.get('exit_code',0))\n"
    )
    binary.chmod(0o755)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": project},
        subagents_enabled=True,
        max_subagents=2,
        environment={"CODEX_HOME": str(codex_home)},
    )
    monkeypatch.setattr(subagent_module, "executable", lambda name, config: str(binary))
    processes = Processes(config)
    manager = Subagents(config, processes)
    try:
        yield manager, Workspace(project), binary
    finally:
        await processes.close()


async def test_parallel_final_results_and_task_isolation(agents):
    manager, workspace, binary = agents
    started = await asyncio.gather(
        manager.spawn("one", "task", workspace, '{"final":"first","delay":0.2}', "First"),
        manager.spawn("two", "task", workspace, '{"final":"second","delay":0.2}', "Second"),
    )
    assert all(result["status"] == "running" for result in started)
    assert len(manager.processes.active) == 2
    waited = await manager.wait(["one", "two"], "task", 2)
    assert not waited["timed_out"]
    await manager.processes.wait("two", "task", 2)
    await manager.processes.wait("one", "task", 2)
    assert {result["final"] for result in manager.list("task")} == {"first", "second"}
    assert all(result["status"] == "completed" for result in manager.list("task"))
    assert manager.get("one", "task")["usage"]["input_tokens"] == 3
    assert manager.list("other") == []
    with pytest.raises(ValueError, match="not found in this task"):
        manager.get("one", "other")
    with pytest.raises(ValueError, match="not found in this task"):
        await manager.cancel("one", "other")
    with pytest.raises(ValueError, match="not found in this task"):
        await manager.wait(["one"], "other", 0)
    assert launch_count(binary) == 2


async def test_launch_is_deduplicated_and_prompt_never_enters_argv(agents):
    manager, workspace, binary = agents
    prompt = '{"inspect":true,"delay":0.1}'
    await manager.spawn("once", "task", workspace, prompt, "Inspect", write=True)
    await manager.spawn("once", "task", workspace, prompt, "Inspect", write=True)
    with pytest.raises(ValueError, match="different request"):
        await manager.spawn("once", "task", workspace, '{"final":"changed"}', "Inspect")
    with pytest.raises(ValueError, match="not found in this task"):
        await manager.spawn("once", "other", workspace, prompt, "Inspect", write=True)
    await manager.processes.wait("once", "task", 2)
    result = manager.get("once", "task")
    details = json.loads(result["final"])
    argv = details["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"
    assert all(
        flag in argv for flag in ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules")
    )
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert details["cwd"] == str(workspace.root)
    assert prompt not in manager.processes.row("once", "task")["command"]
    assert "danger-full-access" not in argv
    assert launch_count(binary) == 1


def test_launch_blocks_user_project_provider_hooks_and_recursive_agents(agents):
    manager, workspace, _ = agents
    child = workspace.root / "sub.dir-測試"
    child.mkdir()
    selected = Workspace(child)
    argv, env = manager._command(selected, False)
    settings = {}
    for i, arg in enumerate(argv[:-1]):
        if arg == "-c":
            for key, value in tomllib.loads(argv[i + 1]).items():
                if isinstance(value, dict):
                    settings.setdefault(key, {}).update(value)
                else:
                    settings[key] = value
    assert settings["model_provider"] == "openai"
    assert settings["approval_policy"] == "never"
    assert settings["agents"]["enabled"] is False
    assert settings["sandbox_workspace_write"] == {
        "network_access": False,
        "exclude_tmpdir_env_var": True,
        "exclude_slash_tmp": True,
    }
    assert settings["projects"] == {
        str(path): {"trust_level": "untrusted"} for path in (child, *child.parents)
    }
    disabled = {argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--disable"}
    assert disabled == {
        "hooks",
        "plugins",
        "remote_plugin",
        "apps",
        "multi_agent",
        "multi_agent_v2",
    }
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv and "--cd" not in argv
    assert env == {"CODEX_HOME": manager.config.environment["CODEX_HOME"]}
    assert manager.config.state_dir not in Path(env["CODEX_HOME"]).parents
    manager.config.environment["OPENAI_BASE_URL"] = "https://unexpected.invalid"
    with pytest.raises(ValueError, match="without provider or internal overrides"):
        manager._command(selected, False)


async def test_login_required_and_process_identity_cannot_be_reused(agents):
    manager, workspace, binary = agents
    manager.config.environment["CODEX_HOME"] = str(workspace.root / "missing-home")
    with pytest.raises(ValueError, match="Sign in with the Codex CLI"):
        await manager.spawn("login", "task", workspace, "{}", "Login")
    assert launch_count(binary) == 0
    with manager.db:
        manager.db.execute(
            "INSERT INTO processes(id,task_id,status,command,cwd,created) "
            "VALUES ('collision','task','completed','[]',?,?)",
            (str(workspace.root), time.time()),
        )
    with pytest.raises(ValueError, match="belongs to another process"):
        await manager.spawn("collision", "task", workspace, "{}", "Collision")


async def test_runner_quota_wait_cancel_and_timeout(agents):
    manager, workspace, _ = agents
    await manager.spawn("slow", "task", workspace, '{"delay":60}', "Slow")
    await manager.spawn("other", "other-task", workspace, '{"delay":60}', "Other")
    assert (await manager.wait(["slow"], "task", 0))["timed_out"]
    with pytest.raises(ValueError, match="Too many running subagents"):
        await manager.spawn("full", "task", workspace, '{"final":"never"}', "Full")
    assert manager.get("slow", "task")["write"] is False
    assert (await manager.cancel("slow", "task"))["status"] == "cancelled"
    await manager.spawn("timeout", "task", workspace, '{"delay":60}', "Timeout", timeout_seconds=1)
    await manager.processes.wait("timeout", "task", 3)
    assert manager.get("timeout", "task")["status"] == "timed_out"
    await manager.cancel("other", "other-task")


@pytest.mark.parametrize(
    "scenario",
    [
        {"error": True, "final": "looks complete"},
        {"final": "looks complete", "exit_code": 1},
        {"final": "missing turn", "no_turn": True},
        {},
        {"final": "after huge event", "oversized": True},
    ],
)
async def test_errors_nonzero_and_incomplete_or_oversized_output_fail(agents, scenario):
    manager, workspace, _ = agents
    await manager.spawn("failed", "task", workspace, json.dumps(scenario), "Fail")
    await manager.processes.wait("failed", "task", 3)
    result = manager.get("failed", "task")
    assert result["status"] == "failed"
    assert result["error"]
    if scenario.get("error"):
        assert result["error"] == "quota reached"
    if scenario.get("oversized"):
        assert result["output_truncated"]


async def test_disabled_validation_and_journal_before_spawn(agents, monkeypatch):
    manager, workspace, binary = agents
    manager.config.subagents_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await manager.spawn("disabled", "task", workspace, "{}", "Disabled")
    manager.config.subagents_enabled = True
    for kwargs in ({"prompt": ""}, {"name": ""}, {"timeout_seconds": 0}, {"write": "yes"}):
        values = {"prompt": "{}", "name": "Invalid", **kwargs}
        with pytest.raises(ValueError):
            await manager.spawn("invalid", "task", workspace, **values)

    def unavailable(*args):
        assert manager._row("journal", "task")["status"] == "running"
        raise ValueError("Codex unavailable")

    monkeypatch.setattr(manager, "_command", unavailable)
    with pytest.raises(ValueError, match="unavailable"):
        await manager.spawn("journal", "task", workspace, "{}", "Journal")
    assert manager.get("journal", "task")["status"] == "failed"
    assert (await manager.spawn("journal", "task", workspace, "{}", "Journal"))[
        "status"
    ] == "failed"
    assert launch_count(binary) == 0


async def test_restart_preserves_results_and_never_replays_uncertain_launches(agents):
    manager, workspace, binary = agents
    await manager.spawn("complete", "task", workspace, '{"final":"saved"}', "Complete")
    await manager.processes.wait("complete", "task", 3)
    expected = manager.get("complete", "task")
    # These committed rows represent a crash before spawn and after spawn respectively.
    with manager.db:
        manager.db.execute(
            "INSERT INTO subagents VALUES ('orphan','task','Orphan','hash',0,?,'running',NULL)",
            (time.time(),),
        )
        manager.db.execute(
            "INSERT INTO subagents VALUES ('active','task','Active','hash',0,?,'running',NULL)",
            (time.time(),),
        )
        manager.db.execute(
            "INSERT INTO processes(id,task_id,status,command,cwd,created) "
            "VALUES ('active','task','running','[]',?,?)",
            (str(workspace.root), time.time()),
        )
    await manager.processes.close()
    restored_processes = Processes(manager.config)
    try:
        restored = Subagents(manager.config, restored_processes)
        assert restored.get("complete", "task") == expected
        assert restored.get("orphan", "task")["status"] == "interrupted"
        assert restored.get("active", "task")["status"] == "interrupted"
        assert (await restored.cancel("orphan", "task"))["status"] == "interrupted"
        assert launch_count(binary) == 1
    finally:
        await restored_processes.close()


async def test_aggregate_previews_keep_full_answers_available(agents):
    manager, workspace, _ = agents
    answer = "完整回答" * 5000
    await manager.spawn(
        "long", "task", workspace, json.dumps({"final": answer}, ensure_ascii=False), "Long"
    )
    await manager.processes.wait("long", "task", 3)
    assert manager.get("long", "task")["final"] == answer
    waited = await manager.wait(["long"], "task", 0)
    assert waited["agents"][0]["final_truncated"]
    assert len(waited["agents"][0]["final"]) == 16000
    assert manager.list("task")[0]["final_truncated"]
    assert len(manager.list("task")[0]["final"]) == 2000
