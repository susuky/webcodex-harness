"""Check a fresh installation through its public CLI and a real Server/Runner."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import webcodex


def run(cli: Path, *args: str, env: dict[str, str], cwd: Path) -> str:
    result = subprocess.run(
        [str(cli), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr[-4000:]}")
    return result.stdout


class RetryTemporaryDirectory(tempfile.TemporaryDirectory):
    """Wait for Windows to release SQLite shared-memory mappings during cleanup."""

    def cleanup(self):
        for attempt in range(100):
            try:
                return super().cleanup()
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.1)


def main():
    source = Path(__file__).resolve().parents[2]
    installed = Path(webcodex.__file__).resolve()
    if installed.is_relative_to(source / "src"):
        raise RuntimeError("Use the installed package, not the source checkout")
    cli = Path(sys.executable).with_name("webcodex.exe" if os.name == "nt" else "webcodex")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    processes = []
    with RetryTemporaryDirectory(prefix="webcodex-install-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        config = root / "config"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        version = run(cli, "--version", env=env, cwd=root).strip()
        run(
            cli,
            "init",
            "--project",
            str(project),
            "--config-dir",
            str(config),
            "--port",
            str(port),
            env=env,
            cwd=root,
        )
        client = config / "client.json"
        try:
            with (
                (root / "server.log").open("w", encoding="utf-8") as server_log,
                (root / "runner.log").open("w", encoding="utf-8") as runner_log,
            ):
                for command, filename, output in [
                    ("serve", "server.json", server_log),
                    ("runner", "runner.json", runner_log),
                ]:
                    processes.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "webcodex",
                                command,
                                "--config",
                                str(config / filename),
                            ],
                            cwd=root,
                            env=env,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        )
                    )
                deadline = time.monotonic() + 30
                while True:
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("Server or Runner stopped during installation check")
                    try:
                        projects = json.loads(
                            run(
                                cli,
                                "call",
                                "projects_list",
                                "--config",
                                str(client),
                                env=env,
                                cwd=root,
                            )
                        )["projects"]
                        if any(p["id"] == "local/project" and p["online"] for p in projects):
                            break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Runner did not register within 30 seconds")
                    time.sleep(0.1)
                diagnostics = json.loads(
                    run(cli, "doctor", "--share", "--config-dir", str(config), env=env, cwd=root)
                )
                checks = {item["name"]: item["ok"] for item in diagnostics["checks"]}
                for name in (
                    "server_config",
                    "runner_config",
                    "server_connection",
                    "client_authentication",
                    "runner_connection",
                ):
                    if not checks.get(name):
                        raise RuntimeError(f"Installation diagnostic failed: {name}")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(source / "examples" / "same_turn_demo.py"),
                        "--project",
                        "local/project",
                        "--config",
                        str(client),
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=90,
                )
                if result.returncode or not all(
                    marker in result.stdout
                    for marker in ("demo started", "demo finished", "exit_code: 0")
                ):
                    raise RuntimeError(
                        f"Demo failed: {result.stdout[-2000:]} {result.stderr[-2000:]}"
                    )
                print(
                    json.dumps(
                        {
                            "version": version,
                            "installed_package": str(installed),
                            "project_online": True,
                            "diagnostics": checks,
                            "demo_exit_code": result.returncode,
                        }
                    ),
                    flush=True,
                )
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
