from __future__ import annotations

import asyncio
import base64
import ctypes
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_harness import Harness, call

from webcodex.catalog import OutcomeUnknown
from webcodex.computer import Computer, windows_desktop, windows_text
from webcodex.config import (
    COMPUTER_PERMISSIONS,
    RunnerConfig,
    ServerConfig,
    initialize,
    load_config,
)


def fake_desktop(monkeypatch, computer):
    image = pytest.importorskip("PIL.Image")
    api = SimpleNamespace(
        FAILSAFE=True,
        size=Mock(return_value=(1920, 1080)),
        failSafeCheck=Mock(),
        isValidKey=lambda key: key in {"ctrl", "a", "enter"},
        moveTo=Mock(),
        click=Mock(),
        scroll=Mock(),
        mouseDown=Mock(),
        mouseUp=Mock(),
        keyDown=Mock(),
        keyUp=Mock(),
    )
    grab = SimpleNamespace(grab=Mock(return_value=image.new("RGB", (1920, 1080), "white")))
    monkeypatch.setattr(computer, "_load", lambda: (api, grab))
    monkeypatch.setattr(computer, "_size", lambda api: api.size())
    monkeypatch.setenv("DISPLAY", ":99")
    return api, grab


def test_computer_configuration_is_explicit_and_trust_does_not_enable_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for enabled, trusted in [(False, False), (False, True), (True, False), (True, True)]:
        directory = tmp_path / f"settings-{enabled}-{trusted}"
        initialize(directory, repo, 8765, trusted, enabled)
        server = load_config(directory / "server.json", ServerConfig)
        runner = load_config(directory / "runner.json", RunnerConfig)
        for permissions in [server.clients["owner"].permissions, runner.permissions]:
            assert permissions & COMPUTER_PERMISSIONS == (
                COMPUTER_PERMISSIONS if enabled else set()
            )
        assert ("computer_control" in server.clients["owner"].approval_required) == (not trusted)


def test_headless_and_wayland_report_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    assert "No X11" in Computer().status()["reason"]
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert "Wayland" in Computer().status()["reason"]


def test_windows_desktop_rejects_services_disconnected_and_protected_sessions(monkeypatch):
    desktop = SimpleNamespace(CloseDesktop=Mock())
    service = SimpleNamespace(
        OpenInputDesktop=Mock(return_value=desktop),
        GetUserObjectInformation=Mock(return_value="Default"),
    )
    sessions = SimpleNamespace(
        ProcessIdToSessionId=Mock(return_value=1),
        WTSQuerySessionInformation=Mock(return_value=0),
        WTS_CURRENT_SERVER_HANDLE=0,
        WTSConnectState=8,
        WTSActive=0,
    )
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(DESKTOP_READOBJECTS=1, UOI_NAME=2))
    monkeypatch.setitem(sys.modules, "win32service", service)
    monkeypatch.setitem(sys.modules, "win32ts", sessions)
    windows_desktop()
    desktop.CloseDesktop.assert_called_once()
    service.GetUserObjectInformation.return_value = "Winlogon"
    with pytest.raises(ValueError, match="protected"):
        windows_desktop()
    assert desktop.CloseDesktop.call_count == 2
    sessions.ProcessIdToSessionId.return_value = 0
    with pytest.raises(ValueError, match="active"):
        windows_desktop()
    sessions.ProcessIdToSessionId.return_value = 1
    sessions.WTSQuerySessionInformation.return_value = 4
    with pytest.raises(ValueError, match="active"):
        windows_desktop()


def test_snapshot_coordinates_freshness_and_input_cleanup(monkeypatch):
    computer = Computer()
    api, _ = fake_desktop(monkeypatch, computer)
    snapshot = computer.screenshot("task", 960)
    assert (snapshot["source_width"], snapshot["width"], snapshot["height"]) == (1920, 960, 540)
    image = snapshot["mcp_result"]["content"][0]
    assert len(base64.b64decode(image["data"])) <= 1024 * 1024
    snapshot_id = snapshot["snapshot_id"]
    with pytest.raises(ValueError, match="Stale"):
        computer.perform("different-task", snapshot_id, "move", x=100, y=100)
    with pytest.raises(ValueError, match="Coordinates"):
        computer.perform("task", snapshot_id, "move", x=1920, y=0)
    with pytest.raises(ValueError, match="Unsupported key"):
        computer.perform("task", snapshot_id, "key", keys=["unsupported"])
    with pytest.raises(ValueError, match="printable"):
        computer.perform("task", snapshot_id, "text", text="do not submit\n")
    api.moveTo.assert_not_called()
    computer.perform("task", snapshot_id, "move", x=1500, y=100)
    api.moveTo.assert_called_once_with(1500, 100, duration=0.2)
    with pytest.raises(ValueError, match="Stale"):
        computer.perform("task", snapshot_id, "move", x=100, y=100)
    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    computer.screenshot("another-task", 960)
    with pytest.raises(ValueError, match="Stale"):
        computer.perform("task", snapshot_id, "key", keys=["enter"])
    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    api.size.return_value = (1280, 720)
    with pytest.raises(ValueError, match="Stale"):
        computer.perform("task", snapshot_id, "key", keys=["enter"])
    api.size.return_value = (1920, 1080)
    computer.snapshot = ("task", snapshot_id, (1920, 1080), time.monotonic() - 301)
    with pytest.raises(ValueError, match="Stale"):
        computer.perform("task", snapshot_id, "key", keys=["enter"])

    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    api.keyDown.side_effect = [None, RuntimeError("injected native failure")]
    with pytest.raises(OutcomeUnknown):
        computer.perform("task", snapshot_id, "key", keys=["ctrl", "a"])
    assert [c.args[0] for c in api.keyUp.call_args_list] == ["a", "ctrl"]
    assert api.FAILSAFE and computer.snapshot is None
    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    api.moveTo.side_effect = [None, RuntimeError("injected corner fail-safe")]
    with pytest.raises(OutcomeUnknown):
        computer.perform(
            "task", snapshot_id, "drag", start_x=10, start_y=10, x=20, y=20, duration=0.2
        )
    api.mouseUp.assert_called_once_with(button="left", _pause=False)
    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    api.keyDown.side_effect = None
    api.keyUp.reset_mock()
    api.keyUp.side_effect = [RuntimeError("first release failed"), None]
    with pytest.raises(OutcomeUnknown):
        computer.perform("task", snapshot_id, "key", keys=["ctrl", "a"])
    assert [c.args[0] for c in api.keyUp.call_args_list] == ["a", "ctrl"]


async def test_desktop_lock_survives_awaiting_job_cancellation(monkeypatch):
    computer = Computer()
    api, _ = fake_desktop(monkeypatch, computer)
    snapshot_id = computer.screenshot("task", 960)["snapshot_id"]
    started, release = threading.Event(), threading.Event()

    def move(*args, **kwargs):
        started.set()
        assert release.wait(5)

    api.moveTo.side_effect = move
    job = asyncio.create_task(
        asyncio.to_thread(computer.perform, "task", snapshot_id, "move", x=20, y=20)
    )
    assert await asyncio.to_thread(started.wait, 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    observation = asyncio.create_task(asyncio.to_thread(computer.screenshot, "other-task", 960))
    try:
        await asyncio.sleep(0.05)
        assert not observation.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(observation, 5))["snapshot_id"] != snapshot_id


async def test_mcp_computer_permissions_approval_dedup_and_native_image(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    harness.config.clients["owner"].permissions |= COMPUTER_PERMISSIONS
    harness.config.clients["owner"].approval_required.add("computer_control")
    # Deliberately leave Runner permissions disabled for the first request.
    await harness.start()
    try:
        api, _ = fake_desktop(monkeypatch, harness.runner.computer)
        async with harness.mcp() as client:
            specs = {t.name: t for t in (await client.list_tools()).tools}
            assert specs["computer_screenshot"].annotations.readOnlyHint
            assert specs["computer_click"].annotations.destructiveHint
            assert not specs["computer_click"].annotations.idempotentHint
            task = await call(
                client, "task_create", {"project": "local/project", "title": "desktop"}
            )
            args = {"task_id": task["id"]}
            denied = await call(client, "computer_screenshot", args, error=True)
            assert "not permitted" in denied["result"]["error"]
            harness.runner_config.permissions |= COMPUTER_PERMISSIONS
            shot = await call(client, "computer_screenshot", args)
            native = await client.call_tool("job_get", {"job_id": shot["job_id"]})
            assert native.content[1].type == "image" and native.content[1].mimeType == "image/jpeg"
            assert "mcp_result" not in native.structuredContent["result"]
            click_args = {
                **args,
                "snapshot_id": shot["result"]["snapshot_id"],
                "x": 50,
                "y": 50,
                "request_id": "click-once",
            }
            pending = await call(client, "computer_click", click_args)
            assert pending["status"] == "pending_approval"
            api.click.assert_not_called()
            async with harness.http("a" * 40) as admin:
                response = await admin.post(
                    "/api/admin/approvals/" + pending["job_id"], json={"allow": True}
                )
                assert response.status_code == 200
            result = await call(client, "job_get", {"job_id": pending["job_id"], "wait_seconds": 2})
            assert result["status"] == "succeeded"
            replay = await call(client, "computer_click", click_args)
            assert replay["job_id"] == pending["job_id"]
            api.click.assert_called_once()
            harness.config.clients["owner"].approval_required.clear()
            stale = await call(
                client, "computer_click", {**click_args, "request_id": "stale"}, error=True
            )
            assert stale["status"] == "failed" and "Stale" in stale["result"]["error"]
            shot = await call(client, "computer_screenshot", args)
            api.click.side_effect = OSError("after input dispatch")
            uncertain = await call(
                client,
                "computer_click",
                {
                    **click_args,
                    "snapshot_id": shot["result"]["snapshot_id"],
                    "request_id": "uncertain",
                },
                error=True,
            )
            assert uncertain["status"] == "unknown"
        async with harness.mcp("d" * 40) as reviewer:
            assert not any(
                t.name.startswith("computer_") for t in (await reviewer.list_tools()).tools
            )
            denied = await call(
                reviewer, "computer_screenshot", {"task_id": task["id"]}, error=True
            )
            assert "not permitted" in denied["error"]
    finally:
        await harness.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows INPUT ABI")
def test_windows_unicode_packets_and_partial_dispatch(monkeypatch):
    packets = []

    def send(count, data, size):
        assert size == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        packets.extend(
            (event.type, event.payload.keyboard.scan, event.payload.keyboard.flags)
            for event in data
        )
        return count

    sender = Mock(side_effect=send)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(SendInput=sender))
    windows_text("中😀")
    assert packets == [(1, unit, flags) for unit in [0x4E2D, 0xD83D, 0xDE00] for flags in [4, 6]]
    sender.side_effect = None
    sender.return_value = 1
    with pytest.raises(OutcomeUnknown):
        windows_text("partial")
