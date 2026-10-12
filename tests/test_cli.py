import asyncio
import json
import os
import sys

from webcodex.cli import arguments
from webcodex.config import RunnerConfig, ServerConfig, load_config


async def test_cli_initialization_doctor_and_bom_arguments(tmp_path):
    project = tmp_path / "中文 repo"
    project.mkdir()
    directory = tmp_path / "private settings"

    async def cli(*args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "webcodex",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), 15)
        assert proc.returncode == 0, err.decode(errors="replace")
        return json.loads(out)

    initialized = await cli(
        "init", "--project", str(project), "--config-dir", str(directory), "--port", "9876"
    )
    assert initialized["mcp_url"] == "http://127.0.0.1:9876/mcp"
    assert "token" not in initialized
    assert (
        load_config(directory / "runner.json", RunnerConfig).projects["project"]
        == project.resolve()
    )
    legacy_runner = load_config(directory / "runner.json", RunnerConfig)
    assert not legacy_runner.project_roots and "projects" not in legacy_runner.permissions
    enabled = await cli(
        "allow-root", str(project), "--name", "work", "--trusted", "--config-dir", str(directory)
    )
    assert enabled["project_root"] == "local/work" and enabled["trusted"]
    assert load_config(directory / "server.json", ServerConfig).clients[
        "owner"
    ].trusted_project_roots == ["local/work"]
    roots_directory = tmp_path / "roots settings"
    initialized = await cli(
        "init", "--project-root", str(project), "--config-dir", str(roots_directory)
    )
    assert initialized["project"] is None and initialized["project_root"] == "local/projects"
    root_runner = load_config(roots_directory / "runner.json", RunnerConfig)
    root_client = load_config(roots_directory / "server.json", ServerConfig).clients["owner"]
    assert root_runner.projects == {} and root_runner.project_roots == {"projects": project}
    assert "projects" in root_runner.permissions
    assert root_client.project_roots == ["local/projects"] and root_client.projects == []
    assert "projects" in root_client.permissions & root_client.approval_required
    doctor = await cli("doctor")
    assert doctor["platform"] == sys.platform
    assert doctor["command_shell"] == ("powershell" if os.name == "nt" else "bash")
    assert doctor["supported_runner"]
    assert isinstance(doctor["computer"]["available"], bool)
    desktop_directory = tmp_path / "desktop settings"
    await cli(
        "init", "--project", str(project), "--config-dir", str(desktop_directory), "--computer-use"
    )
    assert {"computer_observe", "computer_control"} <= load_config(
        desktop_directory / "runner.json", RunnerConfig
    ).permissions
    payload = tmp_path / "args.json"
    payload.write_text('\ufeff{"title":"繁體中文"}', encoding="utf-8")
    assert arguments("@" + str(payload)) == {"title": "繁體中文"}
