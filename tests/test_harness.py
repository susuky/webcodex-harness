from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from webcodex.config import Client, RunnerConfig, ServerConfig
from webcodex.runner import Runner
from webcodex.server import create_app


class Harness:
    def __init__(self, directory: Path):
        self.repo = directory / "repo"
        self.repo.mkdir()
        self.config = ServerConfig(
            state_dir=directory / "server",
            admin_token="a" * 40,
            clients={
                "owner": Client(
                    token="c" * 40, projects=["local/project"], approval_required=set()
                ),
                "reviewer": Client(
                    token="d" * 40,
                    projects=["local/project"],
                    approval_required={"execute", "extensions"},
                ),
            },
            runners={"local": "r" * 40},
        )
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.runner_config = RunnerConfig(
            server_url=self.url,
            runner_id="local",
            token="r" * 40,
            state_dir=directory / "runner",
            projects={"project": self.repo},
            environment={"WEBCODEX_PYTHON": sys.executable},
        )

    async def start(self):
        self.app = create_app(self.config)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        deadline = time.monotonic() + 10
        while not self.server.started:
            if self.server_task.done() or time.monotonic() > deadline:
                raise AssertionError("Server failed to start")
            await asyncio.sleep(0.02)
        self.runner = Runner(self.runner_config)
        self.runner_task = asyncio.create_task(self.runner.run())
        async with self.http() as client:
            while time.monotonic() < deadline:
                response = await client.post("/api/call", json={"name": "projects_list"})
                if response.json().get("projects"):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Runner failed to register")

    def http(self, token="c" * 40):
        return httpx.AsyncClient(
            base_url=self.url, headers={"Authorization": "Bearer " + token}, timeout=30
        )

    @asynccontextmanager
    async def mcp(self, token="c" * 40):
        async with self.http(token) as http:
            async with streamable_http_client(self.url + "/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as client:
                    await client.initialize()
                    yield client

    async def close(self):
        self.runner.stop.set()
        await asyncio.wait_for(self.runner_task, 10)
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 10)


@pytest.fixture
async def harness(tmp_path):
    harness = Harness(tmp_path)
    await harness.start()
    try:
        yield harness
    finally:
        await harness.close()


async def call(client, name, args=None, *, error=False):
    response = await client.call_tool(name, args or {})
    if response.structuredContent:
        value = response.structuredContent
        deadline = time.monotonic() + 30
        while value.get("status") in {"queued", "running"} and time.monotonic() < deadline:
            response = await client.call_tool(
                "job_get", {"job_id": value["job_id"], "wait_seconds": 2}
            )
            value = response.structuredContent
        if error:
            assert response.isError, value
        else:
            assert not response.isError, json.dumps(value.get("result", value), ensure_ascii=False)
            assert value.get("status") not in {"queued", "running", "unknown", "failed"}, value
        return value
    assert error and response.isError, response
    return {"error": response.content[0].text}


async def process_result(client, task_id, initial):
    result = initial["result"]
    deadline = time.monotonic() + 20
    while result["status"] == "running" and time.monotonic() < deadline:
        result = (
            await call(
                client,
                "read_process",
                {
                    "task_id": task_id,
                    "process_id": result["process_id"],
                    "wait_seconds": 1,
                },
            )
        )["result"]
    assert result["status"] != "running", result
    return result


async def test_real_mcp_coding_and_extension_loop(harness):
    repo = harness.repo
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8", newline="\n"
    )
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "AGENTS.md").write_text(
        "# 專案規則\n修改計算函式後執行 pytest。\n", encoding="utf-8", newline="\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "calc.py", "test_calc.py", "AGENTS.md"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    shutil.copytree(
        Path(__file__).parents[1] / "examples/plugins/project-tools", repo / "plugin-source"
    )
    skill = repo / "skill-source"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: verify\ndescription: 執行本專案驗證。\n---\n先讀取測試設定，再執行相關測試。\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "tiny.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6UAAAAABJRU5ErkJggg=="
        )
    )

    async with harness.mcp() as client:
        specs = (await client.list_tools()).tools
        assert len(specs) >= 40
        assert not any(t.name in {"approve", "question_answer"} for t in specs)
        task_id = (
            await call(
                client, "task_create", {"project": "local/project", "title": "修正加法並驗證"}
            )
        )["id"]
        info = (await call(client, "workspace_info", {"task_id": task_id}))["result"]
        assert "pytest" in info["instructions"][0]["content"]
        shell = await call(
            client,
            "exec_command",
            {
                "task_id": task_id,
                "command": "Write-Output '繁體中文 shell'"
                if os.name == "nt"
                else "printf '繁體中文 shell'",
            },
        )
        shell_result = await process_result(client, task_id, shell)
        assert shell_result["exit_code"] == 0 and "繁體中文 shell" in shell_result["output"]
        before = await call(
            client,
            "exec_command",
            {"task_id": task_id, "argv": [sys.executable, "-m", "pytest", "-q"]},
        )
        assert (await process_result(client, task_id, before))["exit_code"] == 1
        file = (await call(client, "read_file", {"task_id": task_id, "path": "calc.py"}))["result"]
        patch = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
        await call(
            client,
            "apply_patch",
            {"task_id": task_id, "patch": patch, "expected_sha256": {"calc.py": file["sha256"]}},
        )
        await call(
            client,
            "write_file",
            {
                "task_id": task_id,
                "path": "calc.py",
                "content": "lost update",
                "expected_sha256": file["sha256"],
            },
            error=True,
        )
        after = await call(
            client,
            "exec_command",
            {"task_id": task_id, "argv": [sys.executable, "-m", "pytest", "-q"]},
        )
        tested = await process_result(client, task_id, after)
        assert tested["exit_code"] == 0 and "1 passed" in tested["output"]
        diff = (await call(client, "git_diff", {"task_id": task_id}))["result"]["diff"]
        assert "+    return a + b" in diff
        searched = await call(
            client, "search_files", {"task_id": task_id, "query": "return a + b", "glob": "*.py"}
        )
        assert "calc.py" in (await process_result(client, task_id, searched))["output"]

        command = {
            "task_id": task_id,
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; p=Path('once.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); time.sleep(1); print('done', end='')",
            ],
            "request_id": "same-operation",
            "yield_time_ms": 0,
        }
        first = await call(client, "exec_command", command)
        second = await call(client, "exec_command", command)
        assert first["job_id"] == second["job_id"]
        assert (await process_result(client, task_id, first))["output"] == "done"
        assert (repo / "once.txt").read_text(encoding="utf-8") == "x"
        await call(
            client,
            "exec_command",
            {**command, "argv": [sys.executable, "-c", "print('wrong')"]},
            error=True,
        )

        terminal = await call(
            client,
            "exec_command",
            {
                "task_id": task_id,
                "argv": [sys.executable, "-u", "-c", "print(input('ready> '))"],
                "tty": True,
            },
        )
        pid = terminal["result"]["process_id"]
        await call(
            client,
            "resize_terminal",
            {"task_id": task_id, "process_id": pid, "rows": 40, "columns": 120},
        )
        await call(
            client,
            "write_stdin",
            {"task_id": task_id, "process_id": pid, "chars": "hello-terminal\n"},
        )
        assert "hello-terminal" in (await process_result(client, task_id, terminal))["output"]

        await call(
            client,
            "extension_install",
            {"task_id": task_id, "kind": "skill", "source": "skill-source"},
        )
        names = (await call(client, "skills_list", {"task_id": task_id}))["result"]["skills"]
        assert any(s["name"] == "installed:verify" for s in names)
        loaded = (
            await call(client, "skill_read", {"task_id": task_id, "name": "installed:verify"})
        )["result"]
        assert "先讀取" in loaded["content"]
        await call(
            client,
            "extension_install",
            {"task_id": task_id, "kind": "plugin", "source": "plugin-source"},
        )
        plugins = (await call(client, "plugins_list", {"task_id": task_id}))["result"]["plugins"]
        assert plugins[0]["name"] == "project-tools"
        tools = (
            await call(
                client,
                "plugin_tools",
                {"task_id": task_id, "plugin": "project-tools", "server": "project"},
            )
        )["result"]["tools"]
        assert any(t["name"] == "project_stats" for t in tools)
        resources = (
            await call(
                client,
                "plugin_resources",
                {"task_id": task_id, "plugin": "project-tools", "server": "project"},
            )
        )["result"]
        assert resources["resources"][0]["uri"] == "project://readme"
        resource = (
            await call(
                client,
                "plugin_read_resource",
                {
                    "task_id": task_id,
                    "plugin": "project-tools",
                    "server": "project",
                    "uri": "project://readme",
                },
            )
        )["result"]
        assert "README.md" in resource["contents"][0]["text"]
        prompts = (
            await call(
                client,
                "plugin_prompts",
                {"task_id": task_id, "plugin": "project-tools", "server": "project"},
            )
        )["result"]
        assert prompts["prompts"][0]["name"] == "summarize_project"
        prompt = (
            await call(
                client,
                "plugin_get_prompt",
                {
                    "task_id": task_id,
                    "plugin": "project-tools",
                    "server": "project",
                    "name": "summarize_project",
                    "arguments": {"focus": "測試"},
                },
            )
        )["result"]
        assert "測試" in prompt["messages"][0]["content"]["text"]
        for number in [1, 2]:
            result = await call(
                client,
                "plugin_call",
                {
                    "task_id": task_id,
                    "plugin": "project-tools",
                    "server": "project",
                    "tool_name": "project_stats",
                    "arguments": {},
                },
            )
            assert result["result"]["provider_result"]["observations"] == number
        await call(
            client,
            "plugin_call",
            {
                "task_id": task_id,
                "plugin": "project-tools",
                "server": "project",
                "tool_name": "missing_tool",
                "arguments": {},
            },
            error=True,
        )
        raw = await client.call_tool("view_image", {"task_id": task_id, "path": "tiny.png"})
        if raw.structuredContent["status"] != "succeeded":
            raw = await client.call_tool(
                "job_get", {"job_id": raw.structuredContent["job_id"], "wait_seconds": 10}
            )
        assert any(c.type == "image" for c in raw.content)
        await call(
            client,
            "task_update",
            {
                "task_id": task_id,
                "status": "active",
                "summary": "加法已修正，pytest 通過；下一步審查。",
                "plan": [{"step": "修正加法", "status": "completed"}],
            },
        )

    async with harness.mcp() as resumed:
        task = await call(resumed, "task_get", {"task_id": task_id})
        assert "pytest 通過" in task["summary"] and task["plan"][0]["status"] == "completed"
        assert (await call(resumed, "task_events", {"task_id": task_id}))["events"]
        await call(
            resumed,
            "plugin_disconnect",
            {"task_id": task_id, "plugin": "project-tools", "server": "project"},
        )
        await call(
            resumed,
            "extension_remove",
            {"task_id": task_id, "kind": "plugin", "name": "project-tools"},
        )
        assert (await call(resumed, "plugins_list", {"task_id": task_id}))["result"][
            "plugins"
        ] == []


async def test_authentication_approval_and_task_isolation(harness):
    async with httpx.AsyncClient(base_url=harness.url) as anonymous:
        assert (await anonymous.post("/mcp", json={})).status_code == 401
        assert (await anonymous.get("/healthz")).status_code == 200
    async with harness.http("r" * 40) as runner_token:
        assert (await runner_token.get("/api/tools")).status_code == 401
    async with harness.http() as owner:
        assert (await owner.get("/api/admin/approvals")).status_code == 401
        assert (await owner.post("/api/runner/register", json={})).status_code == 401
        assert (
            await owner.get("/api/tools", headers={"Origin": "https://untrusted.invalid"})
        ).status_code == 403
        assert (
            await owner.get("/api/tools", headers={"Host": "untrusted.invalid"})
        ).status_code == 400
        assert (await owner.post("/api/call", json={})).status_code == 400
        assert (
            await owner.post("/api/call", content=b"x" * (4 * 1024 * 1024 + 1))
        ).status_code == 413
    async with harness.mcp() as owner, harness.mcp("d" * 40) as reviewer:
        owner_task = (
            await call(owner, "task_create", {"project": "local/project", "title": "owner"})
        )["id"]
        await call(reviewer, "task_get", {"task_id": owner_task}, error=True)
        task = (
            await call(reviewer, "task_create", {"project": "local/project", "title": "review"})
        )["id"]
        job = await call(
            reviewer,
            "exec_command",
            {
                "task_id": task,
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('approved.txt').write_text('approved')",
                ],
            },
        )
        assert job["status"] == "pending_approval"
        assert not (harness.repo / "approved.txt").exists()
        async with harness.http("a" * 40) as admin:
            rows = (await admin.get("/api/admin/approvals")).json()["approvals"]
            assert rows[0]["job_id"] == job["job_id"]
            assert (
                await admin.post("/api/admin/approvals/" + job["job_id"], json={"allow": True})
            ).status_code == 200
        done = await call(reviewer, "job_get", {"job_id": job["job_id"], "wait_seconds": 10})
        assert done["status"] == "succeeded"
        await process_result(reviewer, task, done)
        assert (harness.repo / "approved.txt").read_text(encoding="utf-8") == "approved"
        await call(owner, "job_get", {"job_id": job["job_id"]}, error=True)
        question = await call(
            reviewer, "request_user_input", {"task_id": task, "question": "請提供驗收版本。"}
        )
        async with harness.http("a" * 40) as admin:
            assert (
                await admin.post(
                    "/api/admin/questions/" + question["question_id"], json={"answer": "v1"}
                )
            ).status_code == 200
        assert (await call(reviewer, "task_get", {"task_id": task}))["questions"][0][
            "answer"
        ] == "v1"


async def test_stdio_bridge_uses_the_authenticated_http_harness(harness, tmp_path):
    config = tmp_path / "client.json"
    config.write_text(
        json.dumps({"server_url": harness.url, "token": "c" * 40}), encoding="utf-8", newline="\n"
    )
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "webcodex", "bridge", "--config", str(config)]
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as client:
            initialized = await client.initialize()
            assert "task_create" in initialized.instructions
            assert "host execution blocked" in initialized.instructions
            specs = (await client.list_tools()).tools
            assert len(specs) >= 40
            assert "developer MCPs" in next(
                t.description for t in specs if t.name == "projects_list"
            )
            projects = await call(client, "projects_list")
            assert projects["projects"][0]["id"] == "local/project"
            assert projects["projects"][0]["online"] is True

            harness.runner.stop.set()
            await asyncio.wait_for(harness.runner_task, 10)
            # Expire the stopped Runner's heartbeat without waiting for its 15-second lease.
            with harness.app.state.service.store.db as db:
                db.execute("UPDATE runners SET seen=0")
            assert len((await client.list_tools()).tools) == len(specs)
            offline = (await call(client, "projects_list"))["projects"]
            assert offline[0]["id"] == "local/project" and offline[0]["online"] is False

            harness.config.clients["owner"].projects = []
            assert (await call(client, "projects_list"))["projects"] == []


async def test_plugin_http_transport_and_runner_network(harness):
    plugin = harness.repo / "http-plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "http-provider"}), encoding="utf-8", newline="\n"
    )
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": harness.url + "/mcp",
                        "headers": {"Authorization": "Bearer ${TEST_PROVIDER_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    harness.runner.config.environment["TEST_PROVIDER_TOKEN"] = "c" * 40
    async with harness.mcp() as client:
        task = (
            await call(
                client, "task_create", {"project": "local/project", "title": "HTTP provider"}
            )
        )["id"]
        await call(
            client,
            "extension_install",
            {"task_id": task, "kind": "plugin", "source": "http-plugin"},
        )
        specs = (
            await call(
                client,
                "plugin_tools",
                {"task_id": task, "plugin": "http-provider", "server": "remote"},
            )
        )["result"]
        assert any(t["name"] == "projects_list" for t in specs["tools"])
        result = (
            await call(
                client,
                "plugin_call",
                {
                    "task_id": task,
                    "plugin": "http-provider",
                    "server": "remote",
                    "tool_name": "projects_list",
                    "arguments": {},
                },
            )
        )["result"]
        assert result["provider_result"]["projects"][0]["id"] == "local/project"
        fetched = (
            await call(client, "fetch_url", {"task_id": task, "url": harness.url + "/healthz"})
        )["result"]
        assert fetched["status_code"] == 200 and json.loads(fetched["text"])["status"] == "ok"


@pytest.mark.skipif(
    not (shutil.which("cargo") or os.environ.get("WEBCODEX_TEST_RUST_BIN")),
    reason="Cargo toolchain is not available",
)
async def test_runner_executes_real_cargo_test(harness):
    rust_bin = os.environ.get("WEBCODEX_TEST_RUST_BIN")
    if rust_bin:
        harness.runner.config.environment["PATH"] = rust_bin + os.pathsep + os.environ["PATH"]
    async with harness.mcp() as client:
        task = (
            await call(
                client, "task_create", {"project": "local/project", "title": "Rust toolchain"}
            )
        )["id"]
        await call(
            client,
            "write_file",
            {
                "task_id": task,
                "path": "Cargo.toml",
                "content": '[package]\nname = "harness-fixture"\nversion = "0.1.0"\nedition = "2021"\n[lib]\npath = "lib.rs"\n',
            },
        )
        await call(
            client,
            "write_file",
            {
                "task_id": task,
                "path": "lib.rs",
                "content": "#[test]\nfn verifies_addition() { assert_eq!(2 + 3, 5); }\n",
            },
        )
        job = await call(
            client, "exec_command", {"task_id": task, "argv": ["cargo", "test", "--offline"]}
        )
        result = await process_result(client, task, job)
        assert result["exit_code"] == 0 and "1 passed" in result["output"], result["output"]


@pytest.mark.skipif(
    not os.environ.get("WEBCODEX_TEST_SKILL_COMMIT"),
    reason="Pinned HTTPS installation is an opt-in network test",
)
async def test_install_skill_from_pinned_https_git(harness):
    async with harness.mcp() as client:
        task = (
            await call(
                client,
                "task_create",
                {"project": "local/project", "title": "Pinned skill installation"},
            )
        )["id"]
        args = {
            "task_id": task,
            "kind": "skill",
            "source": "https://github.com/anthropics/skills.git",
            "subpath": "skills/internal-comms",
            "ref": "main",
        }
        await call(client, "extension_install", args, error=True)
        args["ref"] = os.environ["WEBCODEX_TEST_SKILL_COMMIT"]
        installed = await call(client, "extension_install", args)
        assert installed["result"]["ref"] == args["ref"]
        skill = (
            await call(client, "skill_read", {"task_id": task, "name": "installed:internal-comms"})
        )["result"]
        assert "internal-comms" in skill["content"]


async def test_server_restart_keeps_tasks_and_running_process(harness):
    async with harness.mcp() as client:
        task = (
            await call(client, "task_create", {"project": "local/project", "title": "restart"})
        )["id"]
        initial = await call(
            client,
            "exec_command",
            {
                "task_id": task,
                "argv": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(2); print('survived', end='')",
                ],
                "yield_time_ms": 0,
            },
        )
        pid = initial["result"]["process_id"]
    harness.server.should_exit = True
    await asyncio.wait_for(harness.server_task, 10)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", harness.port))
    harness.server = uvicorn.Server(
        uvicorn.Config(create_app(harness.config), log_level="error", access_log=False)
    )
    harness.server_task = asyncio.create_task(harness.server.serve(sockets=[sock]))
    deadline = time.monotonic() + 10
    while not harness.server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    async with harness.mcp() as client:
        assert (await call(client, "task_get", {"task_id": task}))["title"] == "restart"
        job = await call(
            client, "read_process", {"task_id": task, "process_id": pid, "wait_seconds": 3}
        )
        assert job["result"]["output"] == "survived"
