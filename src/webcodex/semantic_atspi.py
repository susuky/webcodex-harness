"""Standalone Ubuntu AT-SPI helper; uses the system Python GI installation."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections import deque


class ResolutionError(Exception):
    def __init__(self, code, message, candidates=None):
        super().__init__(message)
        self.code, self.message, self.candidates = code, message, candidates or []


def safe_text(value, limit=256):
    return str(value or "")[:limit]


def matches(item, selector):
    return all(item.get(key) == value for key, value in selector.items())


class AtspiBackend:
    def __init__(self):
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
        except (ImportError, ValueError) as error:
            raise ResolutionError(
                "missing_dependency", "Install python3-gi and gir1.2-atspi-2.0 on Ubuntu"
            ) from error
        self.api = Atspi
        Atspi.init()
        Atspi.set_timeout(600, 1000)

    def identity(self, element):
        # D-Bus unique name and object path survive geometry changes, but not app restart.
        try:
            identity = f"{element.app.bus_name}:{element.path}"
        except (AttributeError, TypeError) as error:
            raise ResolutionError(
                "provider_identity", "AT-SPI did not expose an element identity"
            ) from error
        return "atspi:" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    def record(self, element, *, window=False):
        api = self.api
        states = element.get_state_set()
        native_role = element.get_role_name()
        protected = native_role in {"password text", "password_text"}
        protected_state = getattr(api.StateType, "PROTECTED", None)
        if protected_state is not None:
            protected = protected or states.contains(protected_state)
        role = {
            "push button": "button",
            "check box": "checkbox",
            "radio button": "radio",
            "combo box": "combobox",
            "page tab": "tab",
            "page tab list": "tablist",
            "menu item": "menuitem",
            "frame": "window",
            "dialog": "window",
        }.get(native_role, native_role)
        interfaces = element.get_interfaces()
        editable = "EditableText" in interfaces
        if editable or protected:
            role = "edit"
        enabled = states.contains(api.StateType.ENABLED) and states.contains(
            api.StateType.SENSITIVE
        )
        focused = states.contains(api.StateType.FOCUSED)
        action_iface = element.get_action_iface() if "Action" in interfaces else None
        action_names = (
            [
                safe_text(action_iface.get_action_name(index), 64)
                for index in range(min(action_iface.get_n_actions(), 16))
            ]
            if action_iface
            else []
        )
        attrs = element.get_attributes() or {}
        if not isinstance(attrs, dict):
            attrs = dict(value.split(":", 1) for value in attrs if ":" in value)
        try:
            auto_id = element.get_accessible_id() if hasattr(element, "get_accessible_id") else ""
        except Exception:
            auto_id = ""
        auto_id = auto_id or attrs.get("id", "")
        row = {
            "name": "[protected]" if protected else safe_text(element.get_name()),
            "role": safe_text(role, 64),
            "automation_id": safe_text(auto_id),
            "element_id": self.identity(element),
            "pid": element.get_process_id(),
            "enabled": enabled,
            "focused": focused,
            "protected": protected,
            "can_invoke": enabled
            and not protected
            and bool(set(action_names) & {"click", "press", "activate"}),
            "can_focus": enabled and states.contains(api.StateType.FOCUSABLE),
            "can_set_text": enabled
            and editable
            and not protected
            and states.contains(api.StateType.EDITABLE),
            "actions": action_names,
        }
        if window:
            row["window_id"] = row.pop("element_id")
        # Never query Text/Value: names and affordances are enough to locate controls.
        return row

    def children(self, element):
        count = element.get_child_count()
        for index in range(min(count, 2000)):
            child = element.get_child_at_index(index)
            if child is not None:
                yield child

    def windows(self, limit):
        result, truncated = [], False
        desktop = self.api.get_desktop(0)
        for app in self.children(desktop):
            for window in self.children(app):
                if len(result) >= limit:
                    truncated = True
                    return result, truncated
                try:
                    row = self.record(window, window=True)
                    result.append((window, row))
                except Exception:
                    # A failed provider read can hide another window with the same name/PID.
                    truncated = True
        return result, truncated

    def perform(self, element, action, text):
        if action == "focus":
            return element.get_component_iface().grab_focus()
        if action == "set_text":
            return element.get_editable_text_iface().set_text_contents(text)
        action_iface = element.get_action_iface()
        for index in range(min(action_iface.get_n_actions(), 16)):
            if action_iface.get_action_name(index) in {"click", "press", "activate"}:
                return action_iface.do_action(index)
        return False


def walk(backend, root, max_depth, max_nodes):
    queue, output, truncated = deque([(root, 0, None)]), [], False
    deadline = time.monotonic() + 10
    while queue:
        if len(output) >= max_nodes or time.monotonic() > deadline:
            truncated = True
            break
        element, depth, parent_id = queue.popleft()
        try:
            row = backend.record(element)
            row.update(depth=depth, parent_id=parent_id)
            output.append((element, row))
            if row.get("protected"):
                continue
            children = iter(backend.children(element))
            if depth >= max_depth:
                if next(children, None) is not None:
                    truncated = True
                continue
            for child in children:
                if len(queue) + len(output) >= max_nodes:
                    truncated = True
                    break
                queue.append((child, depth + 1, row["element_id"]))
        except Exception:
            # A disappearing node prevents proving selector uniqueness for actions.
            truncated = True
    return output, truncated


def execute(request, backend):
    action = request["action"]
    windows, windows_truncated = backend.windows(
        min(request["max_nodes"], 200) if action == "windows" else 200
    )
    if action == "windows":
        return {
            "ok": True,
            "backend": "atspi",
            "windows": [row for _, row in windows],
            "truncated": windows_truncated,
        }
    selected = [(node, row) for node, row in windows if matches(row, request["window"])]
    if not selected:
        raise ResolutionError("window_not_found", "No window matches the selector")
    if len(selected) != 1 or (windows_truncated and "window_id" not in request["window"]):
        raise ResolutionError(
            "ambiguous_window", "Select one window by window_id", [row for _, row in selected[:20]]
        )
    window, window_row = selected[0]
    nodes, truncated = walk(backend, window, request["max_depth"], request["max_nodes"])
    if action == "tree":
        return {
            "ok": True,
            "backend": "atspi",
            "window": window_row,
            "elements": [row for _, row in nodes],
            "truncated": truncated,
        }
    selected = [(node, row) for node, row in nodes if matches(row, request["selector"])]
    if action == "find":
        return {
            "ok": True,
            "backend": "atspi",
            "window": window_row,
            "elements": [row for _, row in selected],
            "truncated": truncated,
        }
    if truncated:
        raise ResolutionError(
            "search_incomplete",
            "Increase tree limits to resolve the control uniquely",
            [row for _, row in selected[:20]],
        )
    if not selected:
        raise ResolutionError("element_not_found", "No control matches the selector")
    if len(selected) != 1:
        raise ResolutionError(
            "ambiguous_element",
            "Add role or automation_id to select one control",
            [row for _, row in selected[:20]],
        )
    element, row = selected[0]
    # Refresh native state immediately before dispatch; geometry is never cached.
    current = backend.record(element)
    if not matches(current, request["selector"]) or current["element_id"] != row["element_id"]:
        raise ResolutionError("stale_element", "Control changed; observe it again")
    if not current.get("can_" + action, False):
        raise ResolutionError(
            "unsupported_action", f"This control does not support {action}", [current]
        )
    try:
        accepted = backend.perform(element, action, request.get("text"))
        if not accepted:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": "Native accessibility action did not confirm completion",
            }
    except Exception:
        return {
            "ok": False,
            "outcome": "unknown",
            "error": "Native action response was lost; inspect the control before retrying",
        }
    return {
        "ok": True,
        "backend": "atspi",
        "action": action,
        "input_sent": True,
        "window": window_row,
        "element": current,
    }


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(65536).decode("utf-8"))
        response = execute(request, AtspiBackend())
    except ResolutionError as error:
        response = {
            "ok": False,
            "outcome": "not_started",
            "error_code": error.code,
            "error": error.message,
            "candidates": error.candidates,
        }
    except Exception:
        response = {
            "ok": False,
            "outcome": "not_started",
            "error_code": "provider_unavailable",
            "error": "AT-SPI is unavailable; check the signed-in desktop and accessibility D-Bus service",
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
