"""Opt-in input tests: run only on a disposable, unlocked desktop."""

from __future__ import annotations

import asyncio
import base64
import io
import os
from pathlib import Path

import pytest
from test_harness import Harness, call

from webcodex.config import COMPUTER_PERMISSIONS

pytestmark = pytest.mark.skipif(
    os.environ.get("WEBCODEX_TEST_DESKTOP") != "1",
    reason="Set WEBCODEX_TEST_DESKTOP=1 on a disposable interactive desktop",
)


async def test_real_mcp_desktop_input_and_screenshot(tmp_path):
    import tkinter as tk

    import pyautogui
    from PIL import Image

    root = tk.Tk()
    root.title("WebCodex desktop verification")
    root.geometry("640x460+100+100")
    root.configure(bg="#eef2f6")
    count, scrolls, drags = [], [], []
    tk.Label(root, text="WebCodex desktop verification", bg="#eef2f6", font=("sans", 20)).pack(
        pady=18
    )
    entry = tk.Entry(root, font=("sans", 18), width=35)
    entry.pack(pady=10)
    button = tk.Button(
        root, text="Click to verify", command=lambda: count.append(1), font=("sans", 14)
    )
    button.pack(pady=8)
    canvas = tk.Canvas(root, width=560, height=140, background="white")
    canvas.pack(pady=10)
    canvas.create_text(280, 30, text="Pointer, scroll and drag target", font=("sans", 14))
    canvas.bind("<B1-Motion>", lambda event: drags.append((event.x, event.y)))
    for event in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
        canvas.bind(event, lambda event: scrolls.append(1))
    status = tk.Label(root, text="Verification in progress", bg="#eef2f6", font=("sans", 13))
    status.pack(pady=5)

    async def settle():
        for _ in range(15):
            root.update()
            await asyncio.sleep(0.01)

    async def pump():
        while True:
            root.update()
            await asyncio.sleep(0.001)

    def point(widget):
        return {
            "x": widget.winfo_rootx() + widget.winfo_width() // 2,
            "y": widget.winfo_rooty() + widget.winfo_height() // 2,
        }

    harness = Harness(tmp_path)
    harness.config.clients["owner"].permissions |= COMPUTER_PERMISSIONS
    harness.runner_config.permissions |= COMPUTER_PERMISSIONS
    started = False
    pump_task = asyncio.create_task(pump())
    try:
        root.update()
        root.focus_force()
        await settle()
        await harness.start()
        started = True
        async with harness.mcp() as client:
            task = await call(
                client, "task_create", {"project": "local/project", "title": "live desktop"}
            )
            task_args = {"task_id": task["id"]}
            observed = await call(client, "computer_status", task_args)
            assert observed["result"]["available"], observed

            async def act(name, **args):
                snapshot = await call(
                    client, "computer_screenshot", {**task_args, "max_dimension": 640}
                )
                result = await call(
                    client,
                    name,
                    {**task_args, "snapshot_id": snapshot["result"]["snapshot_id"], **args},
                )
                assert result["result"]["input_sent"]
                await settle()

            await act("computer_click", **point(entry))
            await act("computer_type", text="Hello 繁體中文 Ω")
            assert entry.get() == "Hello 繁體中文 Ω"
            await act("computer_key", keys=["shift", "home"])
            await act("computer_type", text="Unicode 中文 verified")
            assert entry.get() == "Unicode 中文 verified"
            await act("computer_click", **point(button))
            assert len(count) == 1
            await act("computer_move", **point(canvas))
            assert tuple(pyautogui.position()) == tuple(point(canvas).values())
            await act("computer_scroll", **point(canvas), clicks=-2)
            assert scrolls
            pos = point(canvas)
            await act(
                "computer_drag",
                start_x=pos["x"] - 100,
                start_y=pos["y"],
                x=pos["x"] + 100,
                y=pos["y"],
                duration=0.3,
            )
            assert drags
            status.configure(text="Verified: Unicode · click · keys · move · scroll · drag")
            await settle()
            snapshot = await call(client, "computer_screenshot", task_args)
            response = await client.call_tool("job_get", {"job_id": snapshot["job_id"]})
            native = response.content[1]
            assert native.type == "image" and native.mimeType == "image/jpeg"
            data = base64.b64decode(native.data)
            assert Image.open(io.BytesIO(data)).size == (
                snapshot["result"]["width"],
                snapshot["result"]["height"],
            )
            assert len(data) <= 1024 * 1024
            if destination := os.environ.get("WEBCODEX_TEST_SCREENSHOT"):
                await asyncio.to_thread(Path(destination).write_bytes, data)
    finally:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        if started:
            await harness.close()
        root.destroy()
