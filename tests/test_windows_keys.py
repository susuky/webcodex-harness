from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from test_computer import fake_desktop

from webcodex import computer as desktop
from webcodex.catalog import OutcomeUnknown
from webcodex.computer import Computer, windows_extended_key, windows_scroll


@pytest.mark.parametrize(
    ("key", "virtual_key"),
    [
        ("pageup", 0x21),
        ("pgup", 0x21),
        ("pagedown", 0x22),
        ("pgdn", 0x22),
        ("end", 0x23),
        ("home", 0x24),
        ("left", 0x25),
        ("up", 0x26),
        ("right", 0x27),
        ("down", 0x28),
        ("insert", 0x2D),
        ("delete", 0x2E),
        ("del", 0x2E),
    ],
)
def test_navigation_packets_preserve_extended_flag_without_toggling_numlock(
    monkeypatch, key, virtual_key
):
    native = SimpleNamespace(MapVirtualKey=Mock(return_value=0x47), keybd_event=Mock())
    monkeypatch.setitem(sys.modules, "win32api", native)
    windows_extended_key(key)
    windows_extended_key(key, up=True)
    assert native.MapVirtualKey.call_args_list == [call(virtual_key, 0)] * 2
    assert native.keybd_event.call_args_list == [
        call(virtual_key, 0x47, 1, 0),
        call(virtual_key, 0x47, 3, 0),
    ]
    assert all(packet.args[0] != 0x90 for packet in native.keybd_event.call_args_list)


@pytest.fixture
def windows_keys(monkeypatch):
    computer = Computer()
    api, _ = fake_desktop(monkeypatch, computer)
    api.isValidKey = lambda key: key in {"ctrl", "a", "home", "left"}
    native = Mock()
    monkeypatch.setattr(desktop, "windows_extended_key", native)
    monkeypatch.setattr(sys, "platform", "win32")
    snapshot_id = computer.screenshot("task", 640)["snapshot_id"]
    return computer, api, native, snapshot_id


def test_mixed_chord_uses_native_navigation_and_releases_in_reverse(windows_keys):
    computer, api, native, snapshot_id = windows_keys
    events = []
    api.keyDown.side_effect = lambda key: events.append((key, False))
    api.keyUp.side_effect = lambda key, **kwargs: events.append((key, True))

    def navigation(key, *, up=False):
        assert api.FAILSAFE
        events.append((key, up))

    native.side_effect = navigation
    result = computer.perform("task", snapshot_id, "key", keys=["ctrl", "home", "left"])
    assert result["input_sent"] and computer.snapshot is None
    assert events == [
        ("ctrl", False),
        ("home", False),
        ("left", False),
        ("left", True),
        ("home", True),
        ("ctrl", True),
    ]
    api.keyUp.assert_called_once_with("ctrl", _pause=False)
    assert api.FAILSAFE and api.failSafeCheck.call_count == 3


def test_ordinary_windows_keys_keep_pyautogui_path(windows_keys):
    computer, api, native, snapshot_id = windows_keys
    computer.perform("task", snapshot_id, "key", keys=["ctrl", "a"])
    native.assert_not_called()
    assert api.keyDown.call_args_list == [call("ctrl"), call("a")]
    assert api.keyUp.call_args_list == [call("a", _pause=False), call("ctrl", _pause=False)]
    assert api.FAILSAFE


@pytest.mark.parametrize("failure_on_release", [False, True], ids=["down", "up"])
def test_navigation_failure_still_releases_modifier(windows_keys, failure_on_release):
    computer, api, native, snapshot_id = windows_keys

    def failing_navigation(key, *, up=False):
        assert api.FAILSAFE
        if up == failure_on_release:
            raise OSError("injected native key failure")

    native.side_effect = failing_navigation
    with pytest.raises(OutcomeUnknown):
        computer.perform("task", snapshot_id, "key", keys=["ctrl", "home"])
    assert native.call_args_list == [call("home"), call("home", up=True)]
    api.keyUp.assert_called_once_with("ctrl", _pause=False)
    assert api.FAILSAFE and computer.snapshot is None


def test_navigation_release_bypasses_corner_failsafe(windows_keys):
    computer, api, native, snapshot_id = windows_keys
    api.failSafeCheck.side_effect = [None, RuntimeError("injected corner fail-safe")]
    with pytest.raises(OutcomeUnknown):
        computer.perform("task", snapshot_id, "key", keys=["ctrl", "home"])
    native.assert_called_once_with("home", up=True)
    api.keyUp.assert_called_once_with("ctrl", _pause=False)
    assert api.FAILSAFE and computer.snapshot is None


@pytest.mark.parametrize("clicks", [-2, 1])
def test_windows_scroll_uses_one_wheel_delta_per_click(monkeypatch, clicks):
    native = SimpleNamespace(mouse_event=Mock())
    monkeypatch.setitem(sys.modules, "win32api", native)
    windows_scroll(clicks)
    native.mouse_event.assert_called_once_with(0x0800, 0, 0, clicks * 120, 0)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_scroll_moves_to_target_before_using_platform_backend(windows_keys, monkeypatch, platform):
    computer, api, _, snapshot_id = windows_keys
    native = Mock()
    monkeypatch.setattr(desktop, "windows_scroll", native)
    monkeypatch.setattr(sys, "platform", platform)
    events = []
    api.moveTo.side_effect = lambda x, y: events.append(("move", x, y))
    native.side_effect = lambda clicks: events.append(("native", clicks))
    api.scroll.side_effect = lambda clicks: events.append(("pyautogui", clicks))
    computer.perform("task", snapshot_id, "scroll", x=100, y=200, clicks=-2)
    backend = "native" if platform == "win32" else "pyautogui"
    assert events == [("move", 100, 200), (backend, -2)]
    assert api.FAILSAFE and computer.snapshot is None


def test_windows_scroll_failure_is_reported_without_retry(windows_keys, monkeypatch):
    computer, api, _, snapshot_id = windows_keys
    native = Mock(side_effect=OSError("injected wheel failure"))
    monkeypatch.setattr(desktop, "windows_scroll", native)
    with pytest.raises(OutcomeUnknown):
        computer.perform("task", snapshot_id, "scroll", x=100, y=200, clicks=-2)
    native.assert_called_once_with(-2)
    api.scroll.assert_not_called()
    assert api.FAILSAFE and computer.snapshot is None
