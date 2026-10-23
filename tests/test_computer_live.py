"""Opt-in input tests: run only on a disposable, unlocked desktop."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
from pathlib import Path

import pytest
from test_harness import Harness, call

from webcodex.config import COMPUTER_PERMISSIONS

pytestmark = pytest.mark.skipif(
    os.environ.get("WEBCODEX_TEST_DESKTOP") != "1",
    reason="Set WEBCODEX_TEST_DESKTOP=1 on a disposable interactive desktop",
)


@pytest.mark.parametrize("multimonitor", [False, True], ids=["primary", "multimonitor"])
async def test_real_mcp_desktop_input_and_screenshot(tmp_path, multimonitor):
    if multimonitor and (os.name != "nt" or os.environ.get("WEBCODEX_TEST_MULTIMONITOR") != "1"):
        pytest.skip("Set WEBCODEX_TEST_MULTIMONITOR=1 on a Windows desktop with two monitors")
    if os.name == "nt":
        from webcodex.computer import windows_dpi_awareness

        windows_dpi_awareness()

    import tkinter as tk

    import pyautogui
    from PIL import Image

    monitor = None
    if multimonitor:
        from webcodex.computer import Computer

        observed = Computer().status()
        assert observed["available"], observed
        monitors = observed["monitors"]
        secondary = [item for item in monitors if not item["primary"]]
        if not secondary:
            pytest.skip("Windows multi-monitor verification requires at least two active monitors")
        requested = os.environ.get("WEBCODEX_TEST_MONITOR_ID")
        if requested:
            monitor = next((item for item in monitors if item["id"] == requested), None)
            assert monitor is not None, f"Requested monitor is not active: {requested}"
        else:
            monitor = min(
                secondary, key=lambda item: (item["x"] >= 0 and item["y"] >= 0, item["id"])
            )
    origin_x, origin_y = (monitor["x"], monitor["y"]) if monitor else (0, 0)
    capture_args = {"monitor_id": monitor["id"]} if monitor else {}

    root = tk.Tk()
    root.title("WebCodex desktop verification")
    root.geometry("640x460+100+100")
    root.configure(bg="#eef2f6")
    count, scrolls, drags, presses, releases = [], [], [], [], []
    tk.Label(root, text="WebCodex desktop verification", bg="#eef2f6", font=("sans", 20)).pack(
        pady=18
    )
    entry = tk.Entry(root, font=("sans", 18), width=35)
    entry.pack(pady=10)
    key_events = []
    entry.bind(
        "<KeyPress>",
        lambda event: (
            key_events.append((event.keysym, event.state))
            if event.keysym in {"Home", "Shift_L", "Shift_R"}
            else None
        ),
    )
    button = tk.Button(
        root, text="Click to verify", command=lambda: count.append(1), font=("sans", 14)
    )
    button.pack(pady=8)
    canvas = tk.Canvas(root, width=560, height=140, background="white")
    canvas.pack(pady=10)
    canvas.create_text(280, 30, text="Pointer, scroll and drag target", font=("sans", 14))
    if multimonitor:
        canvas.create_rectangle(10, 10, 70, 50, fill="#e34ac7", outline="")
    canvas.bind("<B1-Motion>", lambda event: drags.append((event.x, event.y)))
    canvas.bind("<ButtonPress-1>", lambda event: presses.append((event.x_root, event.y_root)))
    canvas.bind("<ButtonRelease-1>", lambda event: releases.append((event.x_root, event.y_root)))
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
            "x": widget.winfo_rootx() + widget.winfo_width() // 2 - origin_x,
            "y": widget.winfo_rooty() + widget.winfo_height() // 2 - origin_y,
        }

    harness = Harness(tmp_path)
    harness.config.clients["owner"].permissions |= COMPUTER_PERMISSIONS
    harness.runner_config.permissions |= COMPUTER_PERMISSIONS
    started = False
    pump_task = asyncio.create_task(pump())
    try:
        root.update()
        if monitor:
            import win32api
            import win32con
            import win32gui

            hwnd = win32gui.GetAncestor(root.winfo_id(), win32con.GA_ROOT)
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                origin_x + max(0, (monitor["width"] - root.winfo_width()) // 2),
                origin_y + max(0, (monitor["height"] - root.winfo_height()) // 2),
                0,
                0,
                win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
        root.focus_force()
        await settle()
        if monitor:
            from ctypes import wintypes

            selected = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))
            assert selected["Device"] == monitor["id"]
            get_dpi = ctypes.WinDLL("user32").GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            assert get_dpi(hwnd) == monitor["dpi_x"]
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
                    client,
                    "computer_screenshot",
                    {**task_args, **capture_args, "max_dimension": 640},
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
            assert entry.index("insert") == len(entry.get())
            await act("computer_key", keys=["shift", "home"])
            assert entry.selection_present(), key_events
            await act("computer_type", text="Unicode 中文 verified")
            assert entry.get() == "Unicode 中文 verified"
            await act("computer_click", **point(button))
            assert len(count) == 1
            await act("computer_move", **point(canvas))
            pos = point(canvas)
            assert tuple(pyautogui.position()) == (pos["x"] + origin_x, pos["y"] + origin_y)
            canvas.focus_set()
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
            expected_start = (pos["x"] - 100 + origin_x, pos["y"] + origin_y)
            expected_end = (pos["x"] + 100 + origin_x, pos["y"] + origin_y)
            assert presses == [expected_start]
            assert releases == [expected_end]
            assert tuple(pyautogui.position()) == expected_end
            if os.name == "nt":
                import win32api

                assert not win32api.GetAsyncKeyState(1) & 0x8000
            status.configure(text="Verified: Unicode · click · keys · move · scroll · drag")
            await settle()
            snapshot = await call(client, "computer_screenshot", {**task_args, **capture_args})
            response = await client.call_tool("job_get", {"job_id": snapshot["job_id"]})
            native = response.content[1]
            assert native.type == "image" and native.mimeType == "image/jpeg"
            data = base64.b64decode(native.data)
            picture = Image.open(io.BytesIO(data))
            assert picture.size == (
                snapshot["result"]["width"],
                snapshot["result"]["height"],
            )
            assert len(data) <= 1024 * 1024
            if monitor:
                result = snapshot["result"]
                assert result["monitor_id"] == monitor["id"]
                assert (result["origin_x"], result["origin_y"]) == (origin_x, origin_y)
                assert (result["source_width"], result["source_height"]) == (
                    monitor["width"],
                    monitor["height"],
                )
                sample = (
                    round(
                        (canvas.winfo_rootx() + 30 - origin_x) * picture.width / monitor["width"]
                    ),
                    round(
                        (canvas.winfo_rooty() + 30 - origin_y) * picture.height / monitor["height"]
                    ),
                )
                assert all(
                    abs(actual - expected) <= 20
                    for actual, expected in zip(
                        picture.getpixel(sample), (227, 74, 199), strict=True
                    )
                )
            if destination := os.environ.get("WEBCODEX_TEST_SCREENSHOT"):
                await asyncio.to_thread(Path(destination).write_bytes, data)
            print(
                json.dumps(
                    {
                        "monitor": monitor,
                        "clicks_received": len(count),
                        "scrolls_received": len(scrolls),
                        "drag_motion_events": len(drags),
                        "drag_press": presses[0],
                        "drag_release": releases[0],
                        "test_window_dpi": get_dpi(hwnd) if monitor else None,
                        "screenshot_size": picture.size,
                        "screenshot_bytes": len(data),
                    }
                )
            )
            if monitor:
                other = next(item for item in monitors if item["primary"] != monitor["primary"])
                for destination_monitor in (other, monitor):
                    rect = win32gui.GetWindowRect(hwnd)
                    dpi = get_dpi(hwnd)
                    start = (rect[0] + round(160 * dpi / 96), rect[1] + round(16 * dpi / 96))
                    end = (
                        destination_monitor["x"] + destination_monitor["width"] // 2,
                        destination_monitor["y"] + destination_monitor["height"] // 2,
                    )
                    shot = await call(
                        client, "computer_screenshot", {**task_args, "monitor_id": "desktop"}
                    )
                    origin = shot["result"]["origin_x"], shot["result"]["origin_y"]
                    result = await call(
                        client,
                        "computer_drag",
                        {
                            **task_args,
                            "snapshot_id": shot["result"]["snapshot_id"],
                            "start_x": start[0] - origin[0],
                            "start_y": start[1] - origin[1],
                            "x": end[0] - origin[0],
                            "y": end[1] - origin[1],
                            "duration": 0.8,
                        },
                    )
                    assert result["result"]["input_sent"]
                    await settle()
                    assert tuple(win32api.GetCursorPos()) == end
                    assert (
                        win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))["Device"]
                        == destination_monitor["id"]
                    )
                    assert get_dpi(hwnd) == destination_monitor["dpi_x"]
                    assert not win32api.GetAsyncKeyState(1) & 0x8000
                    await call(
                        client, "computer_screenshot", {**task_args, "monitor_id": "desktop"}
                    )
    finally:
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        if started:
            await harness.close()
        root.destroy()
