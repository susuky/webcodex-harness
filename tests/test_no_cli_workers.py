import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from test_harness import Harness, call, process_result

from webcodex.config import RunnerConfig
from webcodex.execution_policy import check_worker_command, check_worker_input, check_worker_tool
from webcodex.extensions import Provider
from webcodex.processes import Processes


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "exec", "test"],
        [r"C:\tools\codex.exe", "exec", "test"],
        ["work.exe", "run", "test"],
        ["bash", "-lc", "CODEX_HOME=/tmp/profile codex exec test"],
        ["bash", "-lc", "if codex exec test; then echo complete; fi"],
        ["powershell", "-Command", "& 'C:\\tools\\codex.exe' exec test"],
        ["npx", "--yes", "@openai/codex", "exec", "test"],
        ["npm", "exec", "--", "codex", "exec", "test"],
        ["npx", "--package=@openai/codex", "codex", "exec", "test"],
        ["uv", "run", "--project", ".", "codex", "exec", "test"],
        ["uvx", "--from", "codex", "codex", "exec", "test"],
        ["sudo", "-n", "codex", "exec", "test"],
        ["sudo", "-u", "test-user", "-n", "codex", "exec", "test"],
        ["tunnel-client", "codex", "exec", "test"],
        ["node", "/fake/node_modules/@openai/codex/bin/codex.js", "exec", "test"],
        ["python", "-c", "import subprocess; subprocess.run(['codex', 'exec', 'test'])"],
        ["node", "-e", "const cp=require('child_process'); cp.spawn('codex',['exec','test'])"],
        ["bash", "-lc", "bash <<'EOF'\ncodex exec test\nEOF"],
    ],
)
async def test_known_worker_launches_never_reach_process_creation(tmp_path, monkeypatch, argv):
    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("A denied worker reached process creation")

    processes = Processes(SimpleNamespace(state_dir=tmp_path, max_processes=16, environment={}))
    monkeypatch.setattr("webcodex.processes.asyncio.create_subprocess_exec", forbidden_spawn)
    monkeypatch.setitem(sys.modules, "webcodex.windows", SimpleNamespace(spawn=forbidden_spawn))
    try:
        with pytest.raises(ValueError, match="External Codex/Work workers are disabled"):
            await processes.start("blocked", "task", argv, tmp_path, 1)
        assert processes.db.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    finally:
        processes.db.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["pytest", "tests/test_no_cli_workers.py"],
        ["uv", "run", "pytest"],
        ["rg", "codex", "README.md"],
        ["npx", "rg", "codex", "README.md"],
        ["uv", "run", "rg", "codex", "docs"],
        ["python", "-c", "print('codex exec test')"],
        ["node", "-e", "console.log('codex exec test')"],
        ["bash", "-lc", "printf '%s' 'codex exec test'"],
        ["bash", "-lc", "cat <<'EOF'\ncodex exec --help\nEOF"],
        ["bash", "-lc", "python - <<'PY'\nprint('codex exec test')\nPY"],
        ["npx", "--yes", "@playwright/mcp@0.0.80", "--headless"],
        ["tunnel-client", "run", "--profile", "webcodex"],
    ],
)
def test_ordinary_commands_and_worker_documentation_are_allowed(argv):
    check_worker_command(argv)


async def test_terminal_worker_input_cannot_be_split_across_calls(tmp_path):
    processes = Processes(SimpleNamespace(state_dir=tmp_path, max_processes=16, environment={}))
    sent = []
    try:
        with processes.db:
            processes.db.execute(
                "INSERT INTO processes(id,task_id,status,command,cwd,created) VALUES (?,?,?,?,?,0)",
                ("shell", "task", "running", json.dumps(["bash"]), str(tmp_path)),
            )
        processes.active["shell"] = {"terminal": SimpleNamespace(write=sent.append)}
        await processes.write("shell", "task", "co", False)
        with pytest.raises(ValueError, match="External Codex/Work"):
            await processes.write("shell", "task", "dex exec test\n", False)
        assert sent == ["co"]
        assert (
            check_worker_input(
                ["python", "-c", "print(input())"], "", "subprocess.run(['codex'])\n"
            )
            == ""
        )
    finally:
        processes.db.close()


@pytest.mark.parametrize(
    "name", ["codex", "codex-reply", "subagent_spawn", "subagent_wait", "work_agent", "work_spawn"]
)
async def test_provider_worker_tools_are_rejected_before_the_queue(name):
    provider = Provider.__new__(Provider)
    provider.task = SimpleNamespace(done=lambda: False)
    provider.queue = asyncio.Queue()
    with pytest.raises(ValueError, match="External Codex/Work"):
        await provider.request("call_tool", name=name, arguments={})
    assert provider.queue.empty()
    check_worker_tool("browser_navigate")


async def test_provider_worker_command_is_rejected_before_start(tmp_path, monkeypatch):
    def forbidden_stdio(*args, **kwargs):
        raise AssertionError("A denied provider reached process creation")

    monkeypatch.setattr("webcodex.extensions.stdio_client", forbidden_stdio)
    with pytest.raises(ValueError, match="External Codex/Work"):
        Provider(
            {"command": "codex", "args": ["mcp-server"]},
            tmp_path,
            tmp_path,
            SimpleNamespace(environment={}),
            tmp_path,
        )


def test_direct_script_launch_checks_worker_calls_but_allows_documentation(tmp_path):
    script = tmp_path / "helper.py"
    script.write_text("import subprocess\nsubprocess.run(['codex', 'exec', 'test'])\n")
    with pytest.raises(ValueError, match="External Codex/Work"):
        check_worker_command([sys.executable, str(script)])
    script.write_text("print('subprocess.run([codex, exec])')\n")
    check_worker_command([sys.executable, str(script)])


def test_powershell_option_values_do_not_hide_the_executed_script(tmp_path):
    script = tmp_path / "worker.ps1"
    script.write_text("codex exec test\n")
    with pytest.raises(ValueError, match="External Codex/Work"):
        check_worker_command(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    script.write_text("Write-Output 'codex exec test'\n")
    check_worker_command(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)])


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
