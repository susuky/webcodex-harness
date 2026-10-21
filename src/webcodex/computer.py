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
        self.snapshot: tuple[str, str, tuple[int, int], float] | None = None

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

    def status(self) -> dict:
        with self.lock:
            try:
                api, _ = self._load()
                width, height = self._size(api)
                return {
                    "available": True,
                    "backend": {"win32": "windows", "darwin": "macos", "linux": "x11"}[
                        sys.platform
                    ],
                    "source_width": width,
                    "source_height": height,
                    "text_input": sys.platform in {"win32", "darwin"}
                    or shutil.which("xdotool") is not None,
                    "scope": "x11_screen" if sys.platform == "linux" else "primary_display",
                    "screenshot_verified": False,
                }
            except Exception as error:
                return {"available": False, "reason": str(error)[:500]}

    def screenshot(self, task_id: str, max_dimension: int) -> dict:
        with self.lock:
            api, grab = self._load()
            size = self._size(api)
            picture = grab.grab(
                bbox=(0, 0, *size),
                **({"xdisplay": os.environ["DISPLAY"]} if sys.platform == "linux" else {}),
            ).convert("RGB")
            # macOS captures Retina pixels; mouse input and snapshot coordinates use points.
            if sys.platform == "darwin" and picture.size != size:
                if picture.width * size[1] != picture.height * size[0]:
                    raise ValueError(
                        "Display geometry changed during capture; take a new screenshot"
                    )
                picture = picture.resize(size)
            if picture.size != size or self._size(api) != size:
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
            self.snapshot = (task_id, snapshot_id, size, time.monotonic())
            return {
                "snapshot_id": snapshot_id,
                "source_width": size[0],
                "source_height": size[1],
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
            size = self._size(api)
            snapshot = self.snapshot
            if (
                not snapshot
                or snapshot[:3] != (task_id, snapshot_id, size)
                or time.monotonic() - snapshot[3] > 300
            ):
                raise ValueError("Stale snapshot; take a fresh computer_screenshot for this task")
            for x, y in [
                (args[k], args[k.replace("x", "y")]) for k in ("x", "start_x") if k in args
            ]:
                if not (0 <= x < size[0] and 0 <= y < size[1]):
                    raise ValueError("Coordinates must be inside the source screenshot dimensions")
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
