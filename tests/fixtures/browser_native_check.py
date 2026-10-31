"""Native Playwright MCP checks against a disposable local site and profile.

python tests/fixtures/browser_native_check.py --node /path/to/node --cli /path/to/cli.js
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

UPLOAD = "WebCodex upload fixture：繁體中文\n".encode()
DOWNLOAD = bytes(range(256)) * 8 + b"\nWebCodex download fixture\n"
PROFILE_MARKER = "webcodex-native-profile-check"
HTML = b"""<!doctype html><meta charset=utf-8><title>WebCodex browser fixture</title>
<label for=upload>Upload fixture</label><input id=upload type=file>
<p id=uploaded>Waiting for file</p><a id=download href=/download>Download fixture</a>
<p id=ready>Loading</p>
<script>
console.log('WEBCODEX_CONSOLE_INFO');console.error('WEBCODEX_CONSOLE_ERROR');
Promise.all([fetch('/probe-ok'),fetch('/probe-fail')]).then(()=>document.querySelector('#ready').textContent='Fixture ready');
document.querySelector('#upload').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  const response=await fetch('/upload',{method:'POST',body:await file.arrayBuffer()});
  document.querySelector('#uploaded').textContent=await response.text();
});
</script>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/download":
            content, kind, code = DOWNLOAD, "application/octet-stream", 200
        elif self.path == "/probe-ok":
            content, kind, code = b"probe-ok", "text/plain", 200
        elif self.path == "/probe-fail":
            content, kind, code = b"expected fixture failure", "text/plain", 418
        elif self.path == "/":
            content, kind, code = HTML, "text/html; charset=utf-8", 200
        else:
            content, kind, code = b"not found", "text/plain", 404
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        if self.path == "/download":
            self.send_header("Content-Disposition", 'attachment; filename="webcodex-fixture.bin"')
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if self.path != "/upload" or length > 65536:
            self.send_error(400)
            return
        data = self.rfile.read(length)
        self.server.uploads.append(data)
        content = ("Uploaded " + str(len(data))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def text(result) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


async def run(args):
    checks = []
    with tempfile.TemporaryDirectory(prefix="webcodex-browser-check-") as directory:
        root = Path(directory)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        source = root / "upload-fixture.txt"
        source.write_bytes(UPLOAD)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.uploads = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        config = {
            "browser": {
                "isolated": False,
                "launchOptions": {"channel": "chrome", "headless": True},
                "contextOptions": {
                    "viewport": {"width": 1100, "height": 700},
                    "acceptDownloads": True,
                },
            },
            "saveSession": False,
            "codegen": "none",
            "timeouts": {"action": 5000, "navigation": 15000},
        }
        if args.chrome:
            config["browser"]["launchOptions"]["executablePath"] = args.chrome
        config_path = root / "browser.config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        parameters = StdioServerParameters(
            command=args.node,
            args=[
                args.cli,
                "--config",
                str(config_path),
                "--user-data-dir",
                str(root / "profile"),
                "--output-dir",
                str(artifacts),
                "--allowed-origins",
                origin,
            ],
            cwd=str(root),
            env={
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "PATH",
                    "HOME",
                    "USER",
                    "LANG",
                    "LC_ALL",
                    "TMPDIR",
                    "TEMP",
                    "TMP",
                    "SYSTEMROOT",
                    "WINDIR",
                    "USERPROFILE",
                    "LOCALAPPDATA",
                    "APPDATA",
                    "PROGRAMFILES",
                    "PROGRAMFILES(X86)",
                    "DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "DBUS_SESSION_BUS_ADDRESS",
                }
            },
        )

        async def check(session, name, arguments):
            result = await session.call_tool(name, arguments)
            if result.isError:
                raise AssertionError(name + ": " + text(result)[:1500])
            return result

        try:
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams, read_timeout_seconds=timedelta(seconds=45)) as session,
            ):
                await session.initialize()
                schemas = {
                    tool.name: tool.inputSchema for tool in (await session.list_tools()).tools
                }
                for name in (
                    "browser_console_messages",
                    "browser_network_requests",
                    "browser_file_upload",
                ):
                    assert name in schemas, name
                checks.append("installed provider exposes console, network and upload")
                await check(session, "browser_navigate", {"url": origin + "/"})
                await check(session, "browser_wait_for", {"text": "Fixture ready"})
                console = text(await check(session, "browser_console_messages", {"level": "info"}))
                assert "WEBCODEX_CONSOLE_INFO" in console and "WEBCODEX_CONSOLE_ERROR" in console
                checks.append("console info and error messages")
                network_schema = schemas["browser_network_requests"].get("properties", {})
                network_args = (
                    {"static": False}
                    if "static" in network_schema
                    else {"includeStatic": False}
                    if "includeStatic" in network_schema
                    else {}
                )
                network = text(await check(session, "browser_network_requests", network_args))
                assert "/probe-ok" in network and "/probe-fail" in network and "418" in network
                checks.append("network success and 418 failure")
                await check(session, "browser_click", {"target": "#upload"})
                await check(session, "browser_file_upload", {"paths": [str(source)]})
                await check(session, "browser_wait_for", {"text": "Uploaded " + str(len(UPLOAD))})
                assert server.uploads == [UPLOAD]
                checks.append("explicit fixture upload bytes and hash")
                await check(session, "browser_click", {"target": "#download"})
                deadline = time.monotonic() + 10
                found = None
                while time.monotonic() < deadline:
                    found = next(
                        (
                            path
                            for path in artifacts.rglob("*")
                            if path.is_file()
                            and path.name.endswith(".bin")
                            and path.stat().st_size == len(DOWNLOAD)
                        ),
                        None,
                    )
                    if found:
                        break
                    await asyncio.sleep(0.05)
                assert found, "Download did not complete in the provider artifact directory"
                assert (
                    hashlib.sha256(found.read_bytes()).digest() == hashlib.sha256(DOWNLOAD).digest()
                )
                checks.append("download completion, size and SHA-256")
                await check(
                    session,
                    "browser_evaluate",
                    {
                        "function": "() => { localStorage.setItem('webcodex-check', '"
                        + PROFILE_MARKER
                        + "'); document.cookie='webcodex_check="
                        + PROFILE_MARKER
                        + "; Max-Age=3600; Path=/'; return true; }"
                    },
                )
                await check(session, "browser_close", {})
            async with (
                stdio_client(parameters) as streams,
                ClientSession(*streams, read_timeout_seconds=timedelta(seconds=45)) as session,
            ):
                await session.initialize()
                await check(session, "browser_navigate", {"url": origin + "/"})
                restored = text(
                    await check(
                        session,
                        "browser_evaluate",
                        {
                            "function": "() => ({local:localStorage.getItem('webcodex-check'), cookie:document.cookie})"
                        },
                    )
                )
                assert restored.count(PROFILE_MARKER) >= 2
                checks.append("profile cookie and localStorage survive provider restart")
                await check(session, "browser_close", {})
            return {
                "passed": len(checks),
                "checks": checks,
                "upload_bytes": len(UPLOAD),
                "upload_sha256": hashlib.sha256(UPLOAD).hexdigest(),
                "download_bytes": len(DOWNLOAD),
                "download_sha256": hashlib.sha256(DOWNLOAD).hexdigest(),
                "schemas": {
                    key: schemas[key]
                    for key in (
                        "browser_console_messages",
                        "browser_network_requests",
                        "browser_file_upload",
                    )
                },
            }
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--chrome")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
