from __future__ import annotations

import builtins
import ctypes
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_computer import fake_desktop
from test_harness import Harness, call

from webcodex import computer as desktop
from webcodex.catalog import OutcomeUnknown
from webcodex.computer import Computer, windows_dpi_awareness, windows_monitors
from webcodex.config import COMPUTER_PERMISSIONS


def monitor(identifier, x, y, width, height, *, primary=False, dpi=96):
    return {
        "id": identifier,
        "primary": primary,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "dpi_x": dpi,
        "dpi_y": dpi,
    }


@pytest.fixture
def displays(monkeypatch):
    computer = Computer()
    api, grab = fake_desktop(monkeypatch, computer)
    image = pytest.importorskip("PIL.Image")
    layout = [
        monitor("main", 0, 0, 1920, 1080, primary=True),
        monitor("left", -1600, 0, 1600, 900, dpi=120),
        monitor("above", 0, -1440, 2560, 1440, dpi=144),
        monitor("portrait", 1920, 0, 1080, 1920, dpi=144),
    ]
    monkeypatch.setattr(computer, "_monitors", Computer._monitors.__get__(computer))
    monkeypatch.setattr(desktop, "windows_monitors", lambda: deepcopy(layout))
    monkeypatch.setattr(sys, "platform", "win32")
    grab.grab.side_effect = lambda *, bbox, **kwargs: image.new(
        "RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "white"
    )
    return computer, api, grab, layout


def test_windows_status_lists_physical_monitors_and_dpi(displays):
    computer, _, _, layout = displays
    status = computer.status()
    assert status["available"] and status["backend"] == "windows"
    assert status["scope"] == "monitor" and status["monitors"] == layout
    assert status["desktop_monitor_id"] == "desktop"
    assert (status["source_width"], status["source_height"]) == (1920, 1080)


@pytest.mark.parametrize("identifier", [None, "main", "left", "above", "portrait"])
def test_selected_capture_uses_global_bbox_and_returns_local_dimensions(displays, identifier):
    computer, _, grab, layout = displays
    selected = next(item for item in layout if item["id"] == (identifier or "main"))
    shot = computer.screenshot("task", 800, identifier)
    assert (shot["monitor_id"], shot["origin_x"], shot["origin_y"]) == (
        selected["id"],
        selected["x"],
        selected["y"],
    )
    assert (shot["source_width"], shot["source_height"]) == (
        selected["width"],
        selected["height"],
    )
    assert max(shot["width"], shot["height"]) == 800
    grab.grab.assert_called_once_with(
        bbox=(
            selected["x"],
            selected["y"],
            selected["x"] + selected["width"],
            selected["y"] + selected["height"],
        ),
        all_screens=True,
        include_layered_windows=True,
    )


@pytest.mark.parametrize("identifier", ["left", "above", "portrait"])
@pytest.mark.parametrize("action", ["move", "click", "scroll", "drag"])
def test_monitor_local_input_is_translated_for_every_pointer_action(displays, identifier, action):
    computer, api, _, layout = displays
    selected = next(item for item in layout if item["id"] == identifier)
    shot = computer.screenshot("task", 800, identifier)
    args = {"x": 200, "y": 120}
    if action == "click":
        args.update(button="right", clicks=2)
    elif action == "scroll":
        args["clicks"] = -2
    elif action == "drag":
        args.update(start_x=100, start_y=80, duration=0.2)
    computer.perform("task", shot["snapshot_id"], action, **args)
    target = (selected["x"] + 200, selected["y"] + 120)
    if action == "click":
        api.click.assert_called_once_with(*target, clicks=2, interval=0.1, button="right")
    elif action == "scroll":
        api.moveTo.assert_called_once_with(*target)
        api.scroll.assert_called_once_with(-2)
    else:
        api.moveTo.assert_called_with(*target, duration=0.2)
        if action == "drag":
            assert api.moveTo.call_args_list[0].args == (
                selected["x"] + 100,
                selected["y"] + 80,
            )
            api.mouseUp.assert_called_once_with(button="left", _pause=False)
    assert computer.snapshot is None


def test_thumbnail_coordinates_are_rescaled_before_monitor_translation(displays):
    computer, api, _, _ = displays
    shot = computer.screenshot("task", 800, "left")
    assert (shot["width"], shot["height"]) == (800, 450)
    x = 200 * shot["source_width"] // shot["width"]
    y = 150 * shot["source_height"] // shot["height"]
    computer.perform("task", shot["snapshot_id"], "move", x=x, y=y)
    api.moveTo.assert_called_once_with(-1200, 300, duration=0.2)


@pytest.mark.parametrize("change", ["unplug", "reposition", "dpi", "primary", "size"])
@pytest.mark.parametrize("identifier", ["left", "desktop"])
def test_any_layout_or_dpi_change_invalidates_snapshot(displays, change, identifier):
    computer, api, _, layout = displays
    shot = computer.screenshot("task", 800, identifier)
    original = deepcopy(layout)
    if change == "unplug":
        layout.pop(1)
    elif change == "reposition":
        layout[2]["x"] += 10
    elif change == "dpi":
        layout[2]["dpi_x"] = layout[2]["dpi_y"] = 192
    elif change == "primary":
        layout[0]["primary"], layout[1]["primary"] = False, True
    else:
        layout[2]["height"] += 10
    with pytest.raises(ValueError, match="Stale snapshot"):
        computer.perform("task", shot["snapshot_id"], "move", x=10, y=10)
    assert computer.snapshot is None
    layout[:] = original
    with pytest.raises(ValueError, match="Stale snapshot"):
        computer.perform("task", shot["snapshot_id"], "move", x=10, y=10)
    api.moveTo.assert_not_called()


def test_monitor_validation_precedes_capture_or_native_input(displays):
    computer, api, grab, _ = displays
    with pytest.raises(ValueError, match="Unknown monitor_id"):
        computer.screenshot("task", 800, "removed")
    grab.grab.assert_not_called()
    shot = computer.screenshot("task", 800, "left")
    for x, y in [(-1, 0), (0, -1), (1600, 0), (0, 900)]:
        with pytest.raises(ValueError, match="Coordinates"):
            computer.perform("task", shot["snapshot_id"], "move", x=x, y=y)
    api.moveTo.assert_not_called()


def test_status_observation_invalidates_changed_layout_even_if_restored(displays):
    computer, api, _, layout = displays
    shot = computer.screenshot("task", 800, "left")
    layout[1]["dpi_x"] = layout[1]["dpi_y"] = 144
    assert computer.status()["available"]
    assert computer.snapshot is None
    layout[1]["dpi_x"] = layout[1]["dpi_y"] = 120
    with pytest.raises(ValueError, match="Stale snapshot"):
        computer.perform("task", shot["snapshot_id"], "move", x=10, y=10)
    api.moveTo.assert_not_called()


def test_layout_change_during_capture_discards_snapshot(displays):
    computer, _, grab, layout = displays
    computer.screenshot("task", 800, "left")
    capture = grab.grab.side_effect

    def changing_capture(**kwargs):
        layout[1]["x"] -= 10
        return capture(**kwargs)

    grab.grab.side_effect = changing_capture
    with pytest.raises(ValueError, match="geometry changed"):
        computer.screenshot("task", 800, "left")
    assert computer.snapshot is None


def test_secondary_monitor_drag_failure_releases_button_and_retains_failsafe(displays):
    computer, api, _, _ = displays
    shot = computer.screenshot("task", 800, "left")
    api.moveTo.side_effect = [None, OSError("injected native failure")]
    with pytest.raises(OutcomeUnknown):
        computer.perform(
            "task", shot["snapshot_id"], "drag", start_x=10, start_y=20, x=30, y=40, duration=0.2
        )
    api.mouseUp.assert_called_once_with(button="left", _pause=False)
    assert api.FAILSAFE and computer.snapshot is None


def test_virtual_desktop_capture_includes_negative_origins_and_all_monitors(displays):
    computer, _, grab, _ = displays
    shot = computer.screenshot("task", 800, "desktop")
    assert (shot["monitor_id"], shot["origin_x"], shot["origin_y"]) == ("desktop", -1600, -1440)
    assert (shot["source_width"], shot["source_height"]) == (4600, 3360)
    assert shot["width"] == 800
    grab.grab.assert_called_once_with(
        bbox=(-1600, -1440, 3000, 1920), all_screens=True, include_layered_windows=True
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_virtual_desktop_drag_crosses_monitors_in_physical_pixels(displays, reverse):
    computer, api, _, _ = displays
    start, end = (-1500, 200), (300, 400)
    if reverse:
        start, end = end, start
    shot = computer.screenshot("task", 800, "desktop")
    computer.perform(
        "task",
        shot["snapshot_id"],
        "drag",
        start_x=start[0] - shot["origin_x"],
        start_y=start[1] - shot["origin_y"],
        x=end[0] - shot["origin_x"],
        y=end[1] - shot["origin_y"],
        duration=0.5,
    )
    assert api.moveTo.call_args_list[0].args == start
    api.moveTo.assert_called_with(*end, duration=0.5)
    api.mouseDown.assert_called_once_with(button="left")
    api.mouseUp.assert_called_once_with(button="left", _pause=False)
    assert computer.snapshot is None


@pytest.mark.parametrize("point", ["start", "end"])
def test_virtual_desktop_rejects_gap_before_sending_input(displays, point):
    computer, api, _, _ = displays
    shot = computer.screenshot("task", 800, "desktop")
    args = dict(start_x=100, start_y=1640, x=1900, y=1840, duration=0.5)
    args.update({"start_x": 800, "start_y": 1340} if point == "start" else {"x": 800, "y": 1340})
    with pytest.raises(ValueError, match="connected monitor"):
        computer.perform("task", shot["snapshot_id"], "drag", **args)
    api.moveTo.assert_not_called()
    api.mouseDown.assert_not_called()
    assert computer.snapshot is not None


def test_virtual_desktop_drag_failure_releases_left_button(displays):
    computer, api, _, _ = displays
    shot = computer.screenshot("task", 800, "desktop")
    api.moveTo.side_effect = [None, OSError("injected cross-monitor failure")]
    with pytest.raises(OutcomeUnknown):
        computer.perform(
            "task",
            shot["snapshot_id"],
            "drag",
            start_x=100,
            start_y=1640,
            x=1900,
            y=1840,
            duration=0.5,
        )
    api.mouseUp.assert_called_once_with(button="left", _pause=False)
    assert api.FAILSAFE and computer.snapshot is None


def dpi_libraries(monkeypatch, *, process_awareness=2):
    user32 = SimpleNamespace(
        SetProcessDpiAwarenessContext=Mock(return_value=True),
        SetThreadDpiAwarenessContext=Mock(return_value=-4),
        GetDpiForWindow=Mock(),
    )

    def process_dpi(handle, result):
        result._obj.value = process_awareness
        return 0

    shcore = SimpleNamespace(
        GetProcessDpiAwareness=Mock(side_effect=process_dpi),
        GetDpiForMonitor=Mock(),
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **kwargs: {"user32": user32, "shcore": shcore}[name],
        raising=False,
    )
    return user32, shcore


def test_dpi_awareness_sets_process_and_each_worker_thread(monkeypatch):
    user32, _ = dpi_libraries(monkeypatch)
    windows_dpi_awareness()
    assert user32.SetProcessDpiAwarenessContext.call_args.args[0].value == ctypes.c_void_p(-4).value
    assert user32.SetThreadDpiAwarenessContext.call_args.args[0].value == ctypes.c_void_p(-4).value
    # The process default can already be set while a fresh worker still needs its own context.
    user32.SetProcessDpiAwarenessContext.return_value = False
    windows_dpi_awareness()
    assert user32.SetThreadDpiAwarenessContext.call_count == 2


def test_incompatible_process_dpi_is_rejected(monkeypatch):
    user32, _ = dpi_libraries(monkeypatch, process_awareness=1)
    user32.SetProcessDpiAwarenessContext.return_value = False
    with pytest.raises(ValueError, match="Restart the Windows Runner"):
        windows_dpi_awareness()
    user32.SetThreadDpiAwarenessContext.assert_not_called()


def test_dpi_initialization_precedes_pyautogui_import(monkeypatch):
    events = []
    api = SimpleNamespace(FAILSAFE=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pyautogui", api)
    monkeypatch.setattr(desktop, "windows_desktop", lambda: events.append("desktop"))
    monkeypatch.setattr(desktop, "windows_dpi_awareness", lambda: events.append("dpi"))
    original = builtins.__import__

    def observe_import(name, *args, **kwargs):
        if name == "pyautogui":
            events.append("pyautogui")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observe_import)
    assert Computer()._load()[0] is api
    assert events == ["desktop", "dpi", "pyautogui"] and api.FAILSAFE


def test_native_monitor_enumeration_uses_hidden_windows_for_exact_dpi_and_cleans_up(monkeypatch):
    user32, _ = dpi_libraries(monkeypatch)
    records = {
        1: {"Device": "main", "Monitor": (0, 0, 1920, 1080), "Flags": 1},
        2: {"Device": "left", "Monitor": (-1600, -200, 0, 700), "Flags": 0},
    }
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(
            EnumDisplayMonitors=lambda: [
                (handle, None, record["Monitor"]) for handle, record in records.items()
            ],
            GetMonitorInfo=records.__getitem__,
        ),
    )
    gui = SimpleNamespace(CreateWindowEx=Mock(side_effect=[11, 12]), DestroyWindow=Mock())
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    user32.GetDpiForWindow.side_effect = [96, 144]
    result = windows_monitors()
    assert result == [
        monitor("left", -1600, -200, 1600, 900, dpi=144),
        monitor("main", 0, 0, 1920, 1080, primary=True),
    ]
    assert [args.args for args in gui.DestroyWindow.call_args_list] == [(11,), (12,)]
    assert [args.args[3:8] for args in gui.CreateWindowEx.call_args_list] == [
        (0x80000000, 960, 540, 1, 1),
        (0x80000000, -800, 250, 1, 1),
    ]
    user32.SetThreadDpiAwarenessContext.assert_not_called()
    gui.CreateWindowEx.side_effect = None
    gui.CreateWindowEx.return_value = 13
    user32.GetDpiForWindow.side_effect = None
    user32.GetDpiForWindow.return_value = 0
    with pytest.raises(ValueError, match="query monitor DPI"):
        windows_monitors()
    gui.DestroyWindow.assert_called_with(13)


async def test_mcp_monitor_selection_schema_and_local_coordinates(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.config.clients["owner"].permissions |= COMPUTER_PERMISSIONS
    harness.runner_config.permissions |= COMPUTER_PERMISSIONS
    await harness.start()
    try:
        computer = harness.runner.computer
        api, grab = fake_desktop(monkeypatch, computer)
        image = pytest.importorskip("PIL.Image")
        layout = [
            monitor("main", 0, 0, 1920, 1080, primary=True),
            monitor("left", -800, 0, 800, 600),
        ]
        monkeypatch.setattr(computer, "_monitors", lambda api: deepcopy(layout))
        grab.grab.return_value = image.new("RGB", (800, 600), "white")
        async with harness.mcp() as client:
            specs = {spec.name: spec for spec in (await client.list_tools()).tools}
            assert "monitor_id" in specs["computer_screenshot"].inputSchema["properties"]
            task = await call(
                client, "task_create", {"project": "local/project", "title": "monitors"}
            )
            shot = await call(
                client, "computer_screenshot", {"task_id": task["id"], "monitor_id": "left"}
            )
            assert shot["result"]["monitor_id"] == "left" and shot["result"]["origin_x"] == -800
            result = await call(
                client,
                "computer_click",
                {
                    "task_id": task["id"],
                    "snapshot_id": shot["result"]["snapshot_id"],
                    "x": 50,
                    "y": 60,
                },
            )
            assert result["result"]["input_sent"]
            api.click.assert_called_once_with(-750, 60, clicks=1, interval=0.1, button="left")
            if sys.platform == "win32":
                grab.grab.return_value = image.new("RGB", (2720, 1080), "white")
                shot = await call(
                    client, "computer_screenshot", {"task_id": task["id"], "monitor_id": "desktop"}
                )
                result = await call(
                    client,
                    "computer_drag",
                    {
                        "task_id": task["id"],
                        "snapshot_id": shot["result"]["snapshot_id"],
                        "start_x": 100,
                        "start_y": 60,
                        "x": 1000,
                        "y": 60,
                    },
                )
                assert result["result"]["input_sent"]
                assert api.moveTo.call_args_list[0].args == (-700, 60)
                api.moveTo.assert_called_with(200, 60, duration=0.5)
                api.mouseUp.assert_called_once_with(button="left", _pause=False)
    finally:
        await harness.close()
