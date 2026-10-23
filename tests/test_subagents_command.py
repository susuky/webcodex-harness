import json
from types import SimpleNamespace

import pytest

import webcodex.subagents as subagent_module
from webcodex.config import RunnerConfig
from webcodex.filesystem import Workspace
from webcodex.subagents import Subagents


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("write", [False, True])
def test_native_windows_sandbox_survives_ignoring_user_config(
    tmp_path, monkeypatch, platform, write
):
    project, home = tmp_path / "project", tmp_path / "codex-home"
    project.mkdir()
    home.mkdir()
    (home / "auth.json").write_text("{}", encoding="utf-8")
    manager = object.__new__(Subagents)
    manager.config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": project},
        environment={"CODEX_HOME": str(home)},
    )
    monkeypatch.setattr(subagent_module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(subagent_module, "executable", lambda *_: "codex")
    argv, _ = manager._command(Workspace(project), write)
    assert ('windows.sandbox="elevated"' in argv) == (platform == "win32")
    assert "--ignore-user-config" in argv
    assert argv[argv.index("--sandbox") + 1] == ("workspace-write" if write else "read-only")
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "danger-full-access" not in argv


def test_completed_response_survives_a_large_tool_output(tmp_path):
    manager = object.__new__(Subagents)
    manager.config = SimpleNamespace(max_output_bytes=subagent_module.MAX_PARSE_BYTES)
    manager.processes = SimpleNamespace(directory=tmp_path)
    events = [
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "x" * 300000},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Verified"}},
        {"type": "turn.completed"},
    ]
    (tmp_path / "large.log").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    result = manager._events("large", terminal=True)
    assert result["turn_completed"] and result["final"] == "Verified"
    assert not result["limited"]
