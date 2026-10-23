from __future__ import annotations

import base64
import ctypes
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import ExitStack

from .catalog import OutcomeUnknown
from .store import new_id

WINDOWS_EXTENDED_KEYS = {
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
}


def windows_extended_key(key: str, *, up: bool = False) -> None:
    import win32api

    virtual_key = WINDOWS_EXTENDED_KEYS[key]
    # Without E0, NumLock treats navigation as numpad input and drops held Shift.
    win32api.keybd_event(
        virtual_key, win32api.MapVirtualKey(virtual_key, 0), 1 | (2 if up else 0), 0
    )


def windows_scroll(clicks: int) -> None:
    import win32api

    win32api.mouse_event(0x0800, 0, 0, clicks * 120, 0)


def macos_desktop(*, control: bool = False):
    try:
        import Quartz
    except ImportError as error:
        raise ValueError(
            "Install macOS computer dependencies with uv sync --extra computer"
        ) from error
    session = Quartz.CGSessionCopyCurrentDictionary()
    if (
        not session
        or not session.get("kCGSessionOnConsoleKey")
        or session.get("CGSSessionScreenIsLocked")
    ):
        raise ValueError("Start the macOS Runner in an active, unlocked desktop session")
    if not Quartz.CGPreflightScreenCaptureAccess():
        raise ValueError(
            "Grant Screen Recording to the app launching the macOS Runner in System Settings "
            "> Privacy & Security, then restart that app"
        )
    if control and not Quartz.CGPreflightPostEventAccess():
        raise ValueError(
            "Grant Accessibility to the app launching the macOS Runner in System Settings "
            "> Privacy & Security, then restart that app"
        )
    return Quartz


def macos_text(text: str) -> None:
    import Quartz

    for char in text:
        events = [Quartz.CGEventCreateKeyboardEvent(None, 0, down) for down in (True, False)]
        if any(event is None for event in events):
            raise OutcomeUnknown(
                "macOS could not create text input; inspect the desktop before retrying"
            )
        for event in events:
            Quartz.CGEventSetFlags(event, 0)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(char.encode("utf-16-le")) // 2, char)
        try:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, events[0])
        finally:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, events[1])


def windows_desktop() -> None:
    import win32con
    import win32service
    import win32ts

    session = win32ts.ProcessIdToSessionId(os.getpid())
    state = win32ts.WTSQuerySessionInformation(
        win32ts.WTS_CURRENT_SERVER_HANDLE, session, win32ts.WTSConnectState
    )
    if session == 0 or state != win32ts.WTSActive:
        raise ValueError("Start the Windows Runner in an active, unlocked desktop session")
    desktop = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
    try:
        if win32service.GetUserObjectInformation(desktop, win32con.UOI_NAME).lower() != "default":
            raise ValueError("The Windows input desktop is locked or protected")
    finally:
        desktop.CloseDesktop()


def windows_dpi_awareness() -> None:
    """Use physical pixels before desktop libraries set a process-wide DPI mode."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    awareness = ctypes.c_int()
    shcore.GetProcessDpiAwareness.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
    shcore.GetProcessDpiAwareness.restype = ctypes.c_long
    if shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) or awareness.value != 2:
        raise ValueError(
            "Restart the Windows Runner before importing desktop libraries; "
            "per-monitor DPI awareness is required"
        )
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if not user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
        raise ctypes.WinError(ctypes.get_last_error())


def windows_monitors() -> list[dict]:
    from ctypes import wintypes

    import win32api
    import win32gui

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    monitors = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        left, top, right, bottom = info["Monitor"]
        # A hidden per-monitor-aware window reports exact DPI, including custom scaling.
        window = win32gui.CreateWindowEx(
            0,
            "STATIC",
            "",
            0x80000000,
            (left + right) // 2,
            (top + bottom) // 2,
            1,
            1,
            0,
            0,
            0,
            None,
        )
        try:
            dpi = user32.GetDpiForWindow(window)
            if not dpi:
                raise ValueError("Windows could not query monitor DPI")
        finally:
            win32gui.DestroyWindow(window)
        monitors.append(
            {
                "id": info["Device"],
                "primary": bool(info["Flags"] & 1),
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "dpi_x": dpi,
                "dpi_y": dpi,
            }
        )
    if not monitors or not any(monitor["primary"] for monitor in monitors):
        raise ValueError("Windows did not report an active primary monitor")
    return sorted(monitors, key=lambda monitor: monitor["id"])


def windows_text(text: str) -> None:
    """Send UTF-16 key pairs without touching the shared clipboard."""
    from ctypes import wintypes

    class Keyboard(ctypes.Structure):
        _fields_ = [
            ("vk", wintypes.WORD),
            ("scan", wintypes.WORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class Mouse(ctypes.Structure):
        _fields_ = [
            ("x", wintypes.LONG),
            ("y", wintypes.LONG),
            ("data", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class Payload(ctypes.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("payload", Payload)]

    data = text.encode("utf-16-le")
    events = []
    for offset in range(0, len(data), 2):
        unit = int.from_bytes(data[offset : offset + 2], "little")
        for flags in (0x4, 0x6):  # KEYEVENTF_UNICODE, then UNICODE | KEYUP
            events.append(Input(1, Payload(keyboard=Keyboard(0, unit, flags, 0, 0))))
    batch = (Input * len(events))(*events)
    send = ctypes.WinDLL("user32", use_last_error=True).SendInput
    send.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send.restype = wintypes.UINT
    if send(len(batch), batch, ctypes.sizeof(Input)) != len(batch):
        raise OutcomeUnknown(
            "Windows did not accept all text input; inspect the desktop before retrying"
        )


class Computer:
    def __init__(self):
        # One physical desktop is shared by all projects. A thread lock remains held
        # even if the awaiting job is cancelled while native input is finishing.
        self.lock = threading.Lock()
        self.snapshot: tuple[str, str, list[dict], float, dict] | None = None

    @staticmethod
    def _size(api) -> tuple[int, int]:
        if sys.platform == "win32":
            return tuple(api.size())
        if sys.platform == "darwin":
            import Quartz

            size = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size
            return int(size.width), int(size.height)
        # PyAutoGUI's X11 size is cached at import. Query the server so display
        # mode changes invalidate existing screenshot coordinates.
        from Xlib.display import Display

        display = Display()
        try:
            geometry = display.screen().root.get_geometry()
            return geometry.width, geometry.height
        finally:
            display.close()

    def _load(self):
        if sys.platform not in {"linux", "win32", "darwin"}:
            raise ValueError("Computer use supports Ubuntu X11, macOS and native Windows desktops")
        if sys.platform == "linux":
            if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
                raise ValueError("Wayland/WSLg is unsupported; use an Ubuntu on Xorg session")
            if not os.environ.get("DISPLAY"):
                raise ValueError("No X11 display; start the Runner inside the desktop session")
        elif sys.platform == "win32":
            windows_desktop()
            windows_dpi_awareness()
        else:
            macos_desktop()
        try:
            if sys.platform == "linux":
                __import__("tkinter")  # Avoid mouseinfo exiting the Runner when Tk is missing.
            import pyautogui
            from PIL import ImageGrab
        except ImportError as error:
            raise ValueError(
                "Install computer dependencies with uv sync --extra computer (Ubuntu also needs python3-tk)"
            ) from error
        pyautogui.FAILSAFE = True
        return pyautogui, ImageGrab

    def _monitors(self, api) -> list[dict]:
        if sys.platform == "win32":
            return windows_monitors()
        width, height = self._size(api)
        return [
            {
                "id": "screen" if sys.platform == "linux" else "primary",
                "primary": True,
                "x": 0,
                "y": 0,
                "width": width,
                "height": height,
            }
        ]

    def status(self) -> dict:
        with self.lock:
            try:
                api, _ = self._load()
                monitors = self._monitors(api)
                if self.snapshot and self.snapshot[2] != monitors:
                    self.snapshot = None
                primary = next(monitor for monitor in monitors if monitor["primary"])
                return {
                    "available": True,
                    "backend": {"win32": "windows", "darwin": "macos", "linux": "x11"}[
                        sys.platform
                    ],
                    "source_width": primary["width"],
                    "source_height": primary["height"],
                    "text_input": sys.platform in {"win32", "darwin"}
                    or shutil.which("xdotool") is not None,
                    "scope": {
                        "linux": "x11_screen",
                        "win32": "monitor",
                        "darwin": "primary_display",
                    }[sys.platform],
                    **(
                        {"monitors": monitors, "desktop_monitor_id": "desktop"}
                        if sys.platform == "win32"
                        else {}
                    ),
                    "screenshot_verified": False,
                }
            except Exception as error:
                return {"available": False, "reason": str(error)[:500]}

    def screenshot(self, task_id: str, max_dimension: int, monitor_id: str | None = None) -> dict:
        with self.lock:
            api, grab = self._load()
            monitors = self._monitors(api)
            monitor = next(
                (
                    monitor
                    for monitor in monitors
                    if monitor["id"] == monitor_id or (monitor_id is None and monitor["primary"])
                ),
                None,
            )
            if sys.platform == "win32" and monitor_id == "desktop":
                left = min(item["x"] for item in monitors)
                top = min(item["y"] for item in monitors)
                monitor = {
                    "id": "desktop",
                    "x": left,
                    "y": top,
                    "width": max(item["x"] + item["width"] for item in monitors) - left,
                    "height": max(item["y"] + item["height"] for item in monitors) - top,
                }
            if monitor is None:
                raise ValueError("Unknown monitor_id; use computer_status to list current monitors")
            size = monitor["width"], monitor["height"]
            left, top = monitor["x"], monitor["y"]
            picture = grab.grab(
                bbox=(left, top, left + size[0], top + size[1]),
                **({"xdisplay": os.environ["DISPLAY"]} if sys.platform == "linux" else {}),
                **(
                    {"all_screens": True, "include_layered_windows": True}
                    if sys.platform == "win32"
                    else {}
                ),
            ).convert("RGB")
            # macOS captures Retina pixels; mouse input and snapshot coordinates use points.
            if sys.platform == "darwin" and picture.size != size:
                if picture.width * size[1] != picture.height * size[0]:
                    raise ValueError(
                        "Display geometry changed during capture; take a new screenshot"
                    )
                picture = picture.resize(size)
            if picture.size != size or self._monitors(api) != monitors:
                self.snapshot = None
                raise ValueError("Display geometry changed during capture; take a new screenshot")
            picture.thumbnail((max_dimension, max_dimension))
            while True:
                buffer = io.BytesIO()
                picture.save(buffer, format="JPEG", quality=80)
                data = buffer.getvalue()
                if len(data) <= 1024 * 1024:
                    break
                picture.thumbnail((max(1, picture.width // 2), max(1, picture.height // 2)))
            snapshot_id = new_id()
            self.snapshot = (task_id, snapshot_id, monitors, time.monotonic(), monitor)
            return {
                "snapshot_id": snapshot_id,
                "source_width": size[0],
                "source_height": size[1],
                "monitor_id": monitor["id"],
                "origin_x": left,
                "origin_y": top,
                "width": picture.width,
                "height": picture.height,
                "expires_in_seconds": 300,
                "mcp_result": {
                    "content": [
                        {
                            "type": "image",
                            "mimeType": "image/jpeg",
                            "data": base64.b64encode(data).decode(),
                        }
                    ]
                },
            }

    @staticmethod
    def _release(api, fn, *args, **kwargs):
        # Release only keys/buttons pressed by this call, including after a corner
        # fail-safe. The shared lock prevents another call from seeing this override.
        previous = api.FAILSAFE
        try:
            api.FAILSAFE = False
            fn(*args, **kwargs, _pause=False)
        finally:
            api.FAILSAFE = previous

    def perform(self, task_id: str, snapshot_id: str, action: str, **args) -> dict:
        with self.lock:
            api, _ = self._load()
            monitors = self._monitors(api)
            snapshot = self.snapshot
            if snapshot and snapshot[2] != monitors:
                self.snapshot = None
            if (
                not snapshot
                or snapshot[:3] != (task_id, snapshot_id, monitors)
                or time.monotonic() - snapshot[3] > 300
            ):
                raise ValueError("Stale snapshot; take a fresh computer_screenshot for this task")
            monitor = snapshot[4]
            size = monitor["width"], monitor["height"]
            for x, y in [
                (args[k], args[k.replace("x", "y")]) for k in ("x", "start_x") if k in args
            ]:
                if not (0 <= x < size[0] and 0 <= y < size[1]):
                    raise ValueError("Coordinates must be inside the source screenshot dimensions")
                if monitor["id"] == "desktop" and not any(
                    item["x"] <= x + monitor["x"] < item["x"] + item["width"]
                    and item["y"] <= y + monitor["y"] < item["y"] + item["height"]
                    for item in monitors
                ):
                    raise ValueError("Coordinates must be on a connected monitor")
            for key in ("x", "start_x"):
                if key in args:
                    args[key] += monitor["x"]
                    args[key.replace("x", "y")] += monitor["y"]
            if action == "key" and any(not api.isValidKey(key) for key in args["keys"]):
                raise ValueError(
                    "Unsupported key name; use PyAutoGUI key names such as ctrl, enter, or esc"
                )
            if action == "text":
                text = args["text"]
                if any(
                    ord(char) < 32 or 0x7F <= ord(char) < 0xA0 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in text
                ):
                    raise ValueError(
                        "Text must contain printable Unicode; use computer_key for tab or enter"
                    )
                if sys.platform == "linux" and not shutil.which("xdotool"):
                    raise ValueError("Install xdotool on the Ubuntu Runner for Unicode text input")
            if sys.platform == "darwin":
                macos_desktop(control=True)
            api.failSafeCheck()
            self.snapshot = None  # One observed state authorizes at most one input operation.
            try:
                if action == "move":
                    api.moveTo(args["x"], args["y"], duration=0.2)
                elif action == "click":
                    api.click(
                        args["x"],
                        args["y"],
                        clicks=args["clicks"],
                        interval=0.1,
                        button=args["button"],
                    )
                elif action == "scroll":
                    api.moveTo(args["x"], args["y"])
                    if sys.platform == "win32":
                        windows_scroll(args["clicks"])
                    else:
                        api.scroll(args["clicks"])
                elif action == "drag":
                    api.moveTo(args["start_x"], args["start_y"])
                    try:
                        api.mouseDown(button="left")
                        api.moveTo(args["x"], args["y"], duration=args["duration"])
                    finally:
                        self._release(api, api.mouseUp, button="left")
                elif action == "key":
                    with ExitStack() as releases:
                        for key in args["keys"]:
                            if sys.platform == "win32" and key in WINDOWS_EXTENDED_KEYS:
                                releases.callback(windows_extended_key, key, up=True)
                                api.failSafeCheck()
                                windows_extended_key(key)
                            else:
                                releases.callback(self._release, api, api.keyUp, key)
                                api.keyDown(key)
                elif action == "text":
                    if sys.platform == "win32":
                        windows_text(args["text"])
                    elif sys.platform == "darwin":
                        macos_text(args["text"])
                    else:
                        # A file/stdin payload cannot become an xdotool command or
                        # process-list argument; no shell or shared clipboard is used.
                        subprocess.run(
                            ["xdotool", "type", "--clearmodifiers", "--delay", "12", "--file", "-"],
                            input=args["text"].encode("utf-8"),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True,
                            timeout=60,
                        )
                else:
                    raise ValueError("Unknown computer action")
            except Exception as error:
                raise OutcomeUnknown(
                    "Desktop input may have been sent; take a fresh screenshot before deciding to retry"
                ) from error
            return {"action": action, "input_sent": True, "snapshot_id": snapshot_id}
