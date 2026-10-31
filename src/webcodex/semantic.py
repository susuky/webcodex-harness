"""Bounded semantic desktop access through native OS accessibility providers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .catalog import OutcomeUnknown

OBSERVE_ACTIONS = {"windows", "tree", "find"}
CONTROL_ACTIONS = {"invoke", "focus", "set_text"}
SELECTOR_FIELDS = {"name", "role", "automation_id"}
WINDOW_FIELDS = {"name", "pid", "window_id"}
MAX_RESPONSE_BYTES = 1024 * 1024


def _selector(value, allowed: set[str], label: str, *, required: bool) -> dict:
    if value is None and not required:
        return {}
    if not isinstance(value, dict) or not value or set(value) - allowed:
        raise ValueError(f"{label} must contain one or more of: {', '.join(sorted(allowed))}")
    result = {}
    for key, item in value.items():
        if key == "pid":
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError("window.pid must be a positive integer")
        elif not isinstance(item, str) or not item or len(item) > 512 or "\0" in item:
            raise ValueError(f"{label}.{key} must be a non-empty string of at most 512 characters")
        result[key] = item
    return result


def validate_request(
    action: str, window=None, selector=None, text=None, *, max_depth=6, max_nodes=200
):
    if action not in OBSERVE_ACTIONS | CONTROL_ACTIONS:
        raise ValueError("Unknown semantic desktop action")
    for key, value, limit in (("max_depth", max_depth, 20), ("max_nodes", max_nodes, 1000)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise ValueError(f"{key} must be between 1 and {limit}")
    window = _selector(window, WINDOW_FIELDS, "window", required=action != "windows")
    selector = _selector(
        selector, SELECTOR_FIELDS, "selector", required=action in CONTROL_ACTIONS | {"find"}
    )
    if action == "windows" and (window or selector):
        raise ValueError("windows does not accept window or selector")
    if action == "tree" and selector:
        raise ValueError("Use find to filter controls by selector")
    if action == "set_text":
        if not isinstance(text, str) or len(text.encode("utf-8")) > 16384 or "\0" in text:
            raise ValueError("text must be UTF-8 text of at most 16384 bytes without NUL")
    elif text is not None:
        raise ValueError("text is supported only by set_text")
    return {
        "action": action,
        "window": window,
        "selector": selector,
        "text": text,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    }


def _command() -> list[str]:
    directory = Path(__file__).resolve().parent
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            raise ValueError("Windows PowerShell 5.1 is required for UI Automation")
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(directory / "semantic_windows.ps1"),
        ]
    if sys.platform == "linux":
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise ValueError(
                "No desktop D-Bus session; run the Runner in the signed-in Ubuntu desktop session"
            )
        # Ubuntu's GI packages live with system Python, independently of the Runner venv.
        python = (
            "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else shutil.which("python3")
        )
        if not python:
            raise ValueError("Install python3, python3-gi and gir1.2-atspi-2.0 on Ubuntu")
        return [python, str(directory / "semantic_atspi.py")]
    raise ValueError("Semantic desktop control supports Windows UI Automation and Ubuntu AT-SPI")


def run_request(request: dict) -> dict:
    command = _command()
    effect = request["action"] in CONTROL_ACTIONS
    try:
        process = subprocess.run(
            command,
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0,
        )
    except subprocess.TimeoutExpired as error:
        if effect:
            raise OutcomeUnknown(
                "Semantic action timed out; inspect the control before deciding to retry"
            ) from error
        raise ValueError(
            "Accessibility provider timed out; narrow the window or tree limits"
        ) from error
    if len(process.stdout) > MAX_RESPONSE_BYTES:
        error = "Accessibility response exceeded its byte limit"
        if effect:
            raise OutcomeUnknown(error)
        raise ValueError(error)
    try:
        result = json.loads(process.stdout.decode("utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("invalid response")
    except (ValueError, UnicodeError) as error:
        if effect:
            raise OutcomeUnknown(
                "Semantic action response was lost; inspect the control before deciding to retry"
            ) from error
        raise ValueError(
            "Accessibility helper failed; check native desktop dependencies"
        ) from error
    if result.get("outcome") == "unknown":
        raise OutcomeUnknown(result.get("error", "Semantic action outcome is unknown"))
    # Definite resolution errors retain bounded candidates in the tool result.
    if process.returncode and result.get("ok") is not False:
        if effect:
            raise OutcomeUnknown("Accessibility helper exited after possible input dispatch")
        raise ValueError("Accessibility helper exited unexpectedly")
    return result
