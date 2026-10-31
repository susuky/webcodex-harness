"""Native UIA acceptance against a disposable WinForms window."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = str(
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)
HELPER = ROOT / "src/webcodex/semantic_windows.ps1"


def call(action, window=None, selector=None, text=None):
    request = {
        "action": action,
        "window": window or {},
        "selector": selector or {},
        "text": text,
        "max_nodes": 500,
        "max_depth": 12,
    }
    command = [
        POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(HELPER),
    ]
    result = subprocess.run(
        command,
        input=json.dumps(request, ensure_ascii=False).encode(),
        capture_output=True,
        timeout=25,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(result.stdout.decode("utf-8-sig"))


def await_file(path, count):
    for _ in range(40):
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            if state["count"] == count:
                return state
        time.sleep(0.05)
    raise AssertionError(f"Expected fixture action count {count}")


with tempfile.TemporaryDirectory(prefix="webcodex-semantic-") as temporary:
    output = Path(temporary) / "result.json"
    fixture = subprocess.Popen(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path(__file__).with_name("semantic_windows_fixture.ps1")),
            "-ResultPath",
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ready = fixture.stdout.readline()
        if not ready:
            raise AssertionError(fixture.stderr.read().decode("utf-8", errors="replace"))
        info = json.loads(ready.decode("utf-8-sig"))
        window = {"pid": info["pid"], "name": info["name"]}
        tree = call("tree", window)
        assert tree["ok"], tree
        assert "fixture-secret-never-return" not in json.dumps(tree), tree
        assert any(node.get("protected") for node in tree["elements"]), tree
        window = {"window_id": tree["window"]["window_id"]}
        assert call(
            "set_text", window, {"automation_id": "message", "role": "edit"}, "移動前 中文"
        )["ok"]
        assert call("invoke", window, {"automation_id": "apply"})["ok"]
        assert await_file(output, 1)["text"] == "移動前 中文"
        assert call("invoke", window, {"automation_id": "move"})["ok"]
        assert call("set_text", window, {"automation_id": "message"}, "移動縮放後 中文")["ok"]
        assert call("invoke", window, {"automation_id": "apply"})["ok"]
        last = await_file(output, 2)
        assert last["text"] == "移動縮放後 中文"
        ambiguous = call("invoke", window, {"name": "Duplicate", "role": "button"})
        assert ambiguous["error_code"] == "ambiguous_element", ambiguous
        assert json.loads(output.read_text(encoding="utf-8-sig"))["count"] == 2
        print(
            json.dumps(
                {
                    "platform": "windows-uia",
                    "passed": [
                        "native_tree",
                        "password_redaction",
                        "unicode_set_text",
                        "invoke",
                        "move_resize_relocate",
                        "duplicate_rejected",
                    ],
                    "geometry": {k: v for k, v in last.items() if k not in {"count", "text"}},
                }
            )
        )
    finally:
        fixture.terminate()
        try:
            fixture.wait(timeout=3)
        except subprocess.TimeoutExpired:
            fixture.kill()
            fixture.wait()
