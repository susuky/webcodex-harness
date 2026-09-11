import asyncio
import json
import os
import sys

from webcodex.cli import arguments
from webcodex.config import RunnerConfig, load_config


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
    doctor = await cli("doctor")
    assert doctor["platform"] == sys.platform
    assert doctor["command_shell"] == ("powershell" if os.name == "nt" else "bash")
    assert doctor["supported_runner"]
    payload = tmp_path / "args.json"
    payload.write_text('\ufeff{"title":"繁體中文"}', encoding="utf-8")
    assert arguments("@" + str(payload)) == {"title": "繁體中文"}
