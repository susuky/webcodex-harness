"""Real browser integration; set WEBCODEX_TEST_BROWSER_MCP to the installed CLI.js."""

import asyncio
import base64
import functools
import json
import os
import re
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_harness import Harness, call

pytestmark = pytest.mark.skipif(
    not os.environ.get("WEBCODEX_TEST_BROWSER_MCP"),
    reason="Set WEBCODEX_TEST_BROWSER_MCP to @playwright/mcp 0.0.80 cli.js; requires Node and Chrome",
)


def text(response):
    return "\n".join(block.text for block in response.content[1:] if block.type == "text")


def result(response):
    match = re.search(r"### Result\s*\n(.*?)(?=\n###|\Z)", text(response), re.S)
    assert match, text(response)
    return json.loads(match[1])


async def test_browser_plugin_through_real_mcp(tmp_path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(Handler, directory=str(Path(__file__).parent / "fixtures")),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    harness = Harness(tmp_path)
    source = harness.repo / "browser-tools"
    shutil.copytree(Path(__file__).parents[1] / "examples/plugins/browser-tools", source)
    harness.runner_config.environment.update(
        BROWSER_NODE=shutil.which("node"),
        BROWSER_MCP_CLI=str(
            await asyncio.to_thread(Path(os.environ["WEBCODEX_TEST_BROWSER_MCP"]).resolve)
        ),
        BROWSER_ALLOWED_ORIGINS=origin,
    )
    await harness.start()
    try:
        async with harness.mcp() as client:

            async def task(title):
                return (
                    await call(client, "task_create", {"project": "local/project", "title": title})
                )["id"]

            one, two = await task("browser one"), await task("browser two")
            await call(
                client,
                "extension_install",
                {"task_id": one, "kind": "plugin", "source": "browser-tools"},
            )
            common = {"plugin": "browser-tools", "server": "browser"}

            async def browse(name, arguments=None, *, task_id=one, error=False, request_id=None):
                job = await call(
                    client,
                    "plugin_call",
                    {
                        **common,
                        "task_id": task_id,
                        "tool_name": name,
                        "arguments": arguments or {},
                        "request_id": request_id,
                    },
                    error=error,
                )
                response = await client.call_tool("job_get", {"job_id": job["job_id"]})
                assert response.isError == error, text(response)
                return response

            tools = (await call(client, "plugin_tools", {**common, "task_id": one}))["result"][
                "tools"
            ]
            allowed = json.loads((source / ".mcp.json").read_text())["mcpServers"]["browser"][
                "allowedTools"
            ]
            assert {tool["name"] for tool in tools} == set(allowed)
            denied = await browse("browser_run_code_unsafe", {"code": "() => 1"}, error=True)
            assert denied.structuredContent["status"] == "failed"
            await browse("browser_navigate", {"url": "file:///etc/passwd"}, error=True)
            blocked = await browse(
                "browser_navigate",
                {"url": origin.replace("127.0.0.1", "localhost") + "/browser.html"},
                error=True,
            )
            assert "allowed" in text(blocked).lower() or "blocked" in text(blocked).lower()
            await browse("browser_navigate", {"url": origin + "/browser.html"})
            page = await browse("browser_snapshot")

            def ref(response, label):
                match = re.search(label + r"[^\n]*\[ref=([^\]]+)\]", text(response))
                assert match, text(response)
                return match[1]

            name_ref = ref(page, 'textbox "姓名"')
            await browse("browser_type", {"target": name_ref, "text": "測試使用者 Ω"})
            button_ref = ref(await browse("browser_snapshot"), 'button "確認資料"')
            first = await browse("browser_click", {"target": button_ref}, request_id="submit-once")
            second = await browse("browser_click", {"target": button_ref}, request_id="submit-once")
            assert first.structuredContent["job_id"] == second.structuredContent["job_id"]
            await browse("browser_wait_for", {"text": "已確認：測試使用者 Ω（1 次）"})
            extracted = result(
                await browse(
                    "browser_evaluate",
                    {
                        "function": "() => Array.from(document.querySelectorAll('tbody tr'), row => ({item: row.cells[0].textContent, quantity: Number(row.cells[1].textContent)}))"
                    },
                )
            )
            assert extracted == [{"item": "鍵盤", "quantity": 2}, {"item": "滑鼠", "quantity": 3}]
            screenshot = await browse("browser_take_screenshot", {"type": "png", "scale": "css"})
            image = next(block for block in screenshot.content if block.type == "image")
            data = base64.b64decode(image.data)
            assert image.mimeType == "image/png" and data.startswith(b"\x89PNG")
            if destination := os.environ.get("WEBCODEX_TEST_BROWSER_SCREENSHOT"):
                await asyncio.to_thread(Path(destination).write_bytes, data)

            await browse("browser_tabs", {"action": "new", "url": origin + "/second.html"})
            assert "第二頁" in text(await browse("browser_tabs", {"action": "list"}))
            await browse("browser_tabs", {"action": "select", "index": 0})
            assert "已確認" in text(await browse("browser_snapshot"))
            await browse("browser_navigate", {"url": origin + "/second.html"})
            await browse("browser_click", {"target": button_ref}, error=True)

            await browse("browser_navigate", {"url": origin + "/browser.html"}, task_id=two)
            state = result(
                await browse(
                    "browser_evaluate",
                    {
                        "function": "() => ({saved: localStorage.getItem('test-session'), cookie: document.cookie})"
                    },
                    task_id=two,
                )
            )
            assert state == {"saved": None, "cookie": ""}
            await browse("browser_close")
            await call(client, "plugin_disconnect", {**common, "task_id": one})
            await browse("browser_navigate", {"url": origin + "/browser.html"})
            assert (
                result(
                    await browse(
                        "browser_evaluate",
                        {"function": "() => localStorage.getItem('test-session')"},
                    )
                )
                is None
            )
            await browse("browser_close")
            await call(client, "plugin_disconnect", {**common, "task_id": one})

            # Operator explicitly enables persistence for this installed plugin.
            installed = harness.runner.extensions["project"].root / "plugin/browser-tools"
            config_file = installed / "browser.config.json"
            config = json.loads(config_file.read_text())
            config["browser"]["isolated"] = False
            config_file.write_text(json.dumps(config))
            manifest_file = installed / ".mcp.json"
            manifest = json.loads(manifest_file.read_text())
            manifest["mcpServers"]["browser"]["args"] += [
                "--user-data-dir",
                "${TASK_STATE_DIR}/profile",
            ]
            manifest_file.write_text(json.dumps(manifest))
            await browse("browser_navigate", {"url": origin + "/browser.html"})
            await browse(
                "browser_evaluate",
                {
                    "function": "() => { localStorage.setItem('test-session', 'remembered'); document.cookie = 'test_session=remembered; path=/; max-age=3600'; }"
                },
            )
            await browse("browser_close")
            await call(client, "plugin_disconnect", {**common, "task_id": one})
            await browse("browser_navigate", {"url": origin + "/browser.html"})
            remembered = result(
                await browse(
                    "browser_evaluate",
                    {
                        "function": "() => ({saved: localStorage.getItem('test-session'), cookie: document.cookie})"
                    },
                )
            )
            assert (
                remembered["saved"] == "remembered"
                and "test_session=remembered" in remembered["cookie"]
            )
            await browse("browser_close", task_id=two)
            await call(client, "plugin_disconnect", {**common, "task_id": two})
            await browse("browser_navigate", {"url": origin + "/browser.html"}, task_id=two)
            assert result(
                await browse(
                    "browser_evaluate",
                    {
                        "function": "() => ({saved: localStorage.getItem('test-session'), cookie: document.cookie})"
                    },
                    task_id=two,
                )
            ) == {"saved": None, "cookie": ""}
            for task_id in (one, two):
                await browse("browser_close", task_id=task_id)
    finally:
        await harness.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
