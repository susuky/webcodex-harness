import sys

from test_harness import Harness, call, process_result

from webcodex.config import RunnerConfig


def test_legacy_settings_cannot_restore_cli_workers(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": project},
        subagents_enabled=True,
        max_subagents=16,
    )
    assert not {"subagents_enabled", "max_subagents"} & config.model_dump().keys()
    assert not hasattr(config, "subagents_enabled")


async def test_mcp_exposes_host_tools_without_a_cli_worker_backend(tmp_path):
    harness = Harness(tmp_path)
    harness.runner_config = RunnerConfig.model_validate(
        {**harness.runner_config.model_dump(), "subagents_enabled": True, "max_subagents": 4}
    )
    await harness.start()
    try:
        assert not hasattr(harness.runner, "subagents")
        async with harness.mcp() as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert not any(name.startswith("subagent_") for name in names)
            task = await call(
                client, "task_create", {"project": "local/project", "title": "Host tools"}
            )
            args = {"task_id": task["id"]}
            store = harness.app.state.service.store
            before = store.one("SELECT COUNT(*) AS count FROM jobs")["count"]
            for name in ("spawn", "get", "wait", "list", "cancel"):
                result = await call(client, "subagent_" + name, args, error=True)
                assert "Unknown tool" in result["error"]
            assert store.one("SELECT COUNT(*) AS count FROM jobs")["count"] == before
            info = await call(client, "workspace_info", args)
            assert "subagents" not in info["result"]
            assert "codex" not in info["result"]["toolchains"]
            job = await call(
                client,
                "exec_command",
                {**args, "argv": [sys.executable, "-c", "print('host-tools-ok')"]},
            )
            result = await process_result(client, task["id"], job)
            assert result["exit_code"] == 0 and "host-tools-ok" in result["output"]
    finally:
        await harness.close()
