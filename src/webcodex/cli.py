from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

import httpx
import uvicorn

from . import __version__
from .config import RunnerConfig, ServerConfig, initialize, load_config, server_url

DEFAULT_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "webcodex-harness"
    if os.name == "nt"
    else Path("~/.config/webcodex-harness").expanduser()
)


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def profile(path: str) -> dict:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))


def arguments(value: str) -> dict:
    if value == "-":
        value = sys.stdin.read()
    elif value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8-sig")
    value = value.lstrip("\ufeff")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Tool arguments must be a JSON object")
    return result


def main():
    if os.name == "nt":
        for stream in [sys.stdin, sys.stdout, sys.stderr]:
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="WebCodex：讓 MCP 用戶端操作你指定的本機專案")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("init", help="建立獨立的 Server、Runner 與用戶端設定")
    setup.add_argument("--project", required=True, type=Path)
    setup.add_argument("--config-dir", default=DEFAULT_DIR, type=Path)
    setup.add_argument("--port", type=int, default=8765)
    setup.add_argument(
        "--trusted", action="store_true", help="允許用戶端直接執行命令與安裝擴充，不另排人工核准"
    )
    setup.add_argument(
        "--computer-use", action="store_true", help="啟用整個桌面的截圖與輸入操作；預設需核准輸入"
    )
    for name, filename in [
        ("serve", "server.json"),
        ("runner", "runner.json"),
        ("bridge", "client.json"),
    ]:
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=str(DEFAULT_DIR / filename))
    call = commands.add_parser("call", help="以 REST 呼叫與 MCP 相同的工具")
    call.add_argument("name")
    call.add_argument("--arguments", default="{}", help="JSON、@檔案路徑，或 - 代表 stdin")
    call.add_argument("--config", default=str(DEFAULT_DIR / "client.json"))
    tools = commands.add_parser("tools", help="列出工具及完整 schema")
    tools.add_argument("--config", default=str(DEFAULT_DIR / "client.json"))
    for name in ["approvals", "approve", "deny", "questions", "answer"]:
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=str(DEFAULT_DIR / "server.json"))
        if name in {"approve", "deny", "answer"}:
            sub.add_argument("id")
        if name == "answer":
            sub.add_argument("text")
    commands.add_parser("doctor", help="檢查這台機器的執行環境")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # State/logs inherit restrictive permissions, including SQLite WAL and process output.
    os.umask(0o077)
    try:
        if args.command == "init":
            output(
                initialize(
                    args.config_dir, args.project, args.port, args.trusted, args.computer_use
                )
            )
        elif args.command == "serve":
            from .server import create_app

            config = load_config(args.config, ServerConfig)
            uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)
        elif args.command == "runner":
            from .runner import run_runner

            asyncio.run(run_runner(load_config(args.config, RunnerConfig)))
        elif args.command == "bridge":
            from .bridge import run_bridge

            config = profile(args.config)
            asyncio.run(run_bridge(config["server_url"], config["token"]))
        elif args.command == "doctor":
            from .computer import Computer

            output(
                {
                    "platform": sys.platform,
                    "python": sys.version.split()[0],
                    "python_executable": sys.executable,
                    "supported_runner": sys.platform in {"linux", "win32"},
                    "wsl": sys.platform == "linux" and "microsoft" in platform.release().lower(),
                    "command_shell": "powershell" if os.name == "nt" else "bash",
                    "terminal": "conpty" if os.name == "nt" else "pty",
                    "config_dir": str(DEFAULT_DIR),
                    "computer": Computer().status(),
                    "tools": {
                        name: shutil.which(name)
                        for name in [
                            "git",
                            "rg",
                            "python3",
                            "python",
                            "pytest",
                            "cargo",
                            "node",
                            "npm",
                            "docker",
                        ]
                    },
                }
            )
        else:
            config = profile(args.config)
            admin = args.command in {"approvals", "approve", "deny", "questions", "answer"}
            url = server_url(config["public_url"] if admin else config["server_url"])
            token = config["admin_token"] if admin else config["token"]
            with httpx.Client(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                timeout=30,
                trust_env=False,
            ) as client:
                if args.command == "call":
                    response = client.post(
                        "/api/call",
                        json={"name": args.name, "arguments": arguments(args.arguments)},
                    )
                elif args.command == "tools":
                    response = client.get("/api/tools")
                elif args.command in {"approvals", "questions"}:
                    response = client.get("/api/admin/" + args.command)
                elif args.command in {"approve", "deny"}:
                    response = client.post(
                        "/api/admin/approvals/" + args.id, json={"allow": args.command == "approve"}
                    )
                else:
                    response = client.post(
                        "/api/admin/questions/" + args.id, json={"answer": args.text}
                    )
                output(response.json())
                if response.is_error:
                    raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(f"錯誤：{error}", file=sys.stderr)
        raise SystemExit(1) from None
