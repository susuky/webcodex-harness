import json
import os
import sys

import pytest

from webcodex.config import RunnerConfig
from webcodex.extensions import Extensions, plugin_manifest
from webcodex.filesystem import Workspace


async def test_provider_tool_policy_and_private_task_state(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "plugin-source"
    (source / ".codex-plugin").mkdir(parents=True)
    (source / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "probe"}))
    (source / "server.py").write_text(
        "import os\nfrom mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('probe')\ncalls = 0\n"
        "@mcp.tool()\ndef observe() -> dict[str, str | int]:\n"
        "    global calls\n    calls += 1\n"
        "    return {'calls': calls, 'state': os.environ['STATE']}\n"
        "@mcp.tool()\ndef hidden() -> str:\n    return 'must not run'\n"
        "mcp.run()\n"
    )
    spec = {
        "command": sys.executable,
        "args": ["${PLUGIN_ROOT}/server.py"],
        "env": {"STATE": "${TASK_STATE_DIR}"},
        "allowedTools": ["observe"],
    }
    (source / ".mcp.json").write_text(json.dumps({"mcpServers": {"probe": spec}}))
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": repo},
        environment={"TASK_STATE_DIR": str(repo / "must-not-use")},
    )
    extensions = Extensions(config, "project")
    workspace = Workspace(repo)
    await extensions.install(workspace, "plugin", "plugin-source", None, ".", None)
    try:
        one = await extensions.provider("one", workspace, "probe", "probe")
        two = await extensions.provider("two", workspace, "probe", "probe")
        listing = await one.request("list_tools")
        assert [tool["name"] for tool in listing["tools"]] == ["observe"]
        first = (await one.request("call_tool", name="observe", arguments={}))["structuredContent"]
        other = (await two.request("call_tool", name="observe", arguments={}))["structuredContent"]
        assert first["state"] != other["state"]
        assert first["state"] == str(one.state_dir)
        assert one.state_dir.is_relative_to(config.state_dir)
        assert not one.state_dir.is_relative_to(repo)
        if os.name != "nt":
            assert one.state_dir.stat().st_mode & 0o777 == 0o700
        with pytest.raises(ValueError, match="not allowed"):
            await one.request("call_tool", name="hidden", arguments={})

        installed = extensions.root / "plugin/probe/.mcp.json"
        spec["allowedTools"] = []
        installed.write_text(json.dumps({"mcpServers": {"probe": spec}}))
        assert await extensions.provider("one", workspace, "probe", "probe") is one
        assert (await one.request("list_tools"))["tools"] == []
        with pytest.raises(ValueError, match="not allowed"):
            await one.request("call_tool", name="observe", arguments={})
        spec["allowedTools"] = ["observe"]
        installed.write_text(json.dumps({"mcpServers": {"probe": spec}}))
        await extensions.provider("one", workspace, "probe", "probe")
        assert (await one.request("call_tool", name="observe", arguments={}))["structuredContent"][
            "calls"
        ] == 2
        await extensions.disconnect("one", "probe", "probe")
        resumed = await extensions.provider("one", workspace, "probe", "probe")
        assert resumed.state_dir == one.state_dir
        with pytest.raises(ValueError, match="Identifier"):
            await extensions.provider("../escape", workspace, "probe", "probe")
        spec["allowedTools"] = "observe"
        installed.write_text(json.dumps({"mcpServers": {"probe": spec}}))
        with pytest.raises(ValueError, match="allowedTools"):
            plugin_manifest(installed.parent)
    finally:
        await extensions.close()
