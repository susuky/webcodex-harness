"""Run with: dbus-run-session -- xvfb-run -a /usr/bin/python3 tests/fixtures/semantic_native_check.py"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "src/webcodex/semantic_atspi.py"


def call(action, window=None, selector=None, text=None):
    request = {
        "action": action,
        "window": window or {},
        "selector": selector or {},
        "text": text,
        "max_nodes": 500,
        "max_depth": 12,
    }
    result = subprocess.run(
        [sys.executable, str(HELPER)],
        input=json.dumps(request).encode(),
        capture_output=True,
        timeout=22,
        check=True,
    )
    return json.loads(result.stdout)


def await_file(path, count):
    for _ in range(40):
        if path.exists():
            state = json.loads(path.read_text())
            if state["count"] == count:
                return state
        time.sleep(0.05)
    raise AssertionError(f"Expected fixture action count {count}")


with tempfile.TemporaryDirectory(prefix="webcodex-semantic-") as temporary:
    output = Path(temporary) / "result.json"
    environment = {
        **os.environ,
        "NO_AT_BRIDGE": "0",
        "GTK_MODULES": "gail:atk-bridge",
        "GDK_BACKEND": "x11",
        "XDG_SESSION_TYPE": "x11",
    }
    environment.pop("WAYLAND_DISPLAY", None)
    fixture = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("semantic_gtk_fixture.py")), str(output)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = fixture.stdout.readline()
        if not ready:
            raise AssertionError(fixture.stderr.read().decode("utf-8", errors="replace"))
        info = json.loads(ready)
        window = {"pid": info["pid"], "name": info["name"]}
        for _ in range(10):
            tree = call("tree", window)
            if tree.get("ok"):
                break
            time.sleep(0.2)
        assert tree["ok"], tree
        assert "fixture-secret-never-return" not in json.dumps(tree), tree
        assert any(node.get("protected") for node in tree["elements"]), tree
        window = {"window_id": tree["window"]["window_id"]}
        assert call("set_text", window, {"name": "Message", "role": "edit"}, "移動前 中文")["ok"]
        assert call("invoke", window, {"name": "Apply", "role": "button"})["ok"]
        assert await_file(output, 1)["text"] == "移動前 中文"
        assert call("invoke", window, {"name": "Move and resize", "role": "button"})["ok"]
        assert call("set_text", window, {"name": "Message", "role": "edit"}, "移動縮放後 中文")[
            "ok"
        ]
        assert call("invoke", window, {"name": "Apply", "role": "button"})["ok"]
        assert await_file(output, 2)["text"] == "移動縮放後 中文"
        ambiguous = call("invoke", window, {"name": "Duplicate", "role": "button"})
        assert ambiguous["error_code"] == "ambiguous_element", ambiguous
        assert json.loads(output.read_text())["count"] == 2
        print(
            json.dumps(
                {
                    "platform": "ubuntu-atspi",
                    "passed": [
                        "native_tree",
                        "password_redaction",
                        "unicode_set_text",
                        "invoke",
                        "move_resize_relocate",
                        "duplicate_rejected",
                    ],
                    "window_id": window["window_id"],
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
