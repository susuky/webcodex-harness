from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from webcodex.catalog import OutcomeUnknown
from webcodex.computer import Computer
from webcodex.semantic import run_request, validate_request
from webcodex.semantic_atspi import AtspiBackend, ResolutionError, execute


class FakeBackend:
    def __init__(self):
        self.window = {"name": "Fixture", "pid": 15, "window_id": "test:15"}
        self.rows = {
            "root": {"name": "Fixture", "role": "window", "element_id": "root"},
            "button": {
                "name": "Save",
                "role": "button",
                "automation_id": "save",
                "element_id": "button",
                "can_invoke": True,
            },
            "edit": {
                "name": "Title",
                "role": "edit",
                "automation_id": "title",
                "element_id": "edit",
                "can_set_text": True,
            },
        }
        self.tree = {"root": ["button", "edit"]}
        self.perform = Mock(return_value=True)

    def windows(self, limit):
        return [("root", self.window)], False

    def record(self, element):
        return dict(self.rows[element])

    def children(self, element):
        return iter(self.tree.get(element, []))


def request(action="invoke", **kwargs):
    return validate_request(action, {"window_id": "test:15"}, {"name": "Save"}, **kwargs)


def test_semantic_revalidates_live_controls_after_movement_and_names_change():
    backend = FakeBackend()
    assert execute(request(), backend)["input_sent"]
    # Geometry has no part in the selector. Reordering the live tree is harmless.
    backend.tree["root"].reverse()
    assert execute(request(), backend)["element"]["automation_id"] == "save"
    backend.rows["button"]["name"] = "Different control"
    with pytest.raises(ResolutionError) as caught:
        execute(request(), backend)
    assert caught.value.code == "element_not_found"
    assert backend.perform.call_count == 2


def test_duplicate_and_partial_search_never_act():
    backend = FakeBackend()
    backend.rows["duplicate"] = {
        **backend.rows["button"],
        "element_id": "duplicate",
        "automation_id": "other",
    }
    backend.tree["root"].append("duplicate")
    with pytest.raises(ResolutionError) as caught:
        execute(request(), backend)
    assert caught.value.code == "ambiguous_element"
    assert len(caught.value.candidates) == 2
    backend.perform.assert_not_called()
    with pytest.raises(ResolutionError) as caught:
        execute(request(max_nodes=2), backend)
    assert caught.value.code == "search_incomplete"
    backend.perform.assert_not_called()
    exact = validate_request("invoke", {"pid": 15}, {"automation_id": "save"})
    assert execute(exact, backend)["ok"]
    backend.perform.assert_called_once()


def test_depth_limit_and_disappearing_nodes_report_incomplete():
    backend = FakeBackend()
    backend.tree["button"] = ["missing"]
    with pytest.raises(ResolutionError) as caught:
        execute(request(max_depth=1), backend)
    assert caught.value.code == "search_incomplete"
    backend.perform.assert_not_called()
    tree = execute(validate_request("tree", {"pid": 15}), backend)
    assert tree["truncated"]


@pytest.mark.parametrize("window_selector", [{"name": "Fixture"}, {"pid": 15}])
def test_unreadable_window_prevents_name_or_pid_action_but_exact_identity_works(window_selector):
    backend = FakeBackend()
    backend.api = SimpleNamespace(get_desktop=lambda index: "desktop")
    backend.tree.update({"desktop": ["app"], "app": ["root", "unavailable"]})
    original_record = backend.record

    def record(element, *, window=False):
        if element == "unavailable":
            # The other window has the same title/PID, but its provider drops the read.
            raise RuntimeError("Window provider disconnected")
        return dict(backend.window) if window else original_record(element)

    backend.record = record
    backend.windows = lambda limit: AtspiBackend.windows(backend, limit)
    observed = execute(validate_request("windows"), backend)
    assert observed["truncated"]
    assert observed["windows"] == [backend.window]
    with pytest.raises(ResolutionError) as caught:
        execute(validate_request("invoke", window_selector, {"name": "Save"}), backend)
    assert caught.value.code == "ambiguous_window"
    backend.perform.assert_not_called()

    assert execute(request(), backend)["input_sent"]
    backend.perform.assert_called_once()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell regression")
def test_uia_unreadable_window_requires_exact_identity(tmp_path):
    source = (Path(__file__).parents[1] / "src/webcodex/semantic_windows.ps1").read_text(
        encoding="utf-8"
    )
    # Load the shipped helpers without opening the desktop or dispatching native input.
    helpers = source.split("\ntry {\n", 1)[0]
    check = r"""
function Row($element, [bool]$isWindow = $false) {
    if ($element -eq 'unavailable') { throw 'Window provider disconnected' }
    return @{name='Fixture'; pid=15; window_id='exact:first'}
}
$walker = [pscustomobject]@{}
$walker | Add-Member -MemberType ScriptMethod -Name GetFirstChild -Value { param($root) return 'readable' }
$walker | Add-Member -MemberType ScriptMethod -Name GetNextSibling -Value {
    param($element)
    if ($element -eq 'readable') { return 'unavailable' }
    return $null
}
$observed = ReadWindows $walker 'desktop' 200
if (-not $observed.truncated -or $observed.items.Count -ne 1) { throw 'Failed read was not marked incomplete' }
foreach ($selector in @([pscustomobject]@{name='Fixture'}, [pscustomobject]@{pid=15})) {
    $script:failure = $null
    try { $null = ResolveWindow $observed.items $selector $observed.truncated } catch { }
    if ($null -eq $script:failure -or $script:failure.error_code -ne 'ambiguous_window') {
        throw 'An incomplete list accepted a name/PID-only selector'
    }
}
$exact = ResolveWindow $observed.items ([pscustomobject]@{window_id='exact:first'}) $observed.truncated
if ($exact.native -ne 'readable') { throw 'An exact window identity was not resolved' }
[Console]::Out.Write('uia-window-enumeration-ok')
"""
    script = tmp_path / "window-enumeration.ps1"
    script.write_text(helpers + check, encoding="utf-8")
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    result = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.decode("utf-8-sig") == "uia-window-enumeration-ok"


def test_dispatch_failure_and_unsupported_control_do_not_retry():
    backend = FakeBackend()
    backend.perform.side_effect = RuntimeError("native effect may have happened")
    assert execute(request(), backend)["outcome"] == "unknown"
    backend.perform.assert_called_once()
    backend.rows["button"]["can_invoke"] = False
    with pytest.raises(ResolutionError) as caught:
        execute(request(), backend)
    assert caught.value.code == "unsupported_action"
    assert backend.perform.call_count == 1


def test_protected_fields_never_query_text_or_name():
    backend = AtspiBackend.__new__(AtspiBackend)
    backend.api = SimpleNamespace(
        StateType=SimpleNamespace(ENABLED=1, SENSITIVE=2, FOCUSED=3, FOCUSABLE=4)
    )
    backend.identity = lambda element: "protected"
    native = SimpleNamespace(
        get_state_set=lambda: SimpleNamespace(contains=lambda state: state in {1, 2}),
        get_role_name=lambda: "password text",
        get_interfaces=lambda: ["EditableText"],
        get_attributes=lambda: {},
        get_accessible_id=lambda: "password",
        get_process_id=lambda: 1,
        get_name=Mock(side_effect=AssertionError("secret read")),
        get_text_iface=Mock(side_effect=AssertionError("secret read")),
    )
    row = backend.record(native)
    assert row["protected"] and row["name"] == "[protected]"
    assert not row["can_set_text"] and not row["can_invoke"]
    native.get_name.assert_not_called()
    native.get_text_iface.assert_not_called()


def test_protected_subtrees_are_not_traversed():
    backend = FakeBackend()
    backend.rows["edit"].update(protected=True, name="[protected]", can_set_text=False)
    backend.tree["edit"] = ["private"]
    backend.rows["private"] = {"name": "private-password", "role": "text", "element_id": "private"}
    result = execute(validate_request("tree", {"pid": 15}), backend)
    assert not result["truncated"]
    assert "private-password" not in json.dumps(result)
    assert len(result["elements"]) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": {}},
        {"window": {"pid": True}},
        {"window": {"name": ""}},
        {"selector": {"index": 0}},
        {"selector": {"name": "x", "shell": "whoami"}},
        {"max_nodes": 0},
        {"max_depth": True},
        {"text": "unexpected"},
    ],
)
def test_validation_rejects_ambiguous_shapes_before_provider(kwargs):
    args = {"action": "invoke", "window": {"pid": 1}, "selector": {"name": "Save"}, **kwargs}
    with pytest.raises(ValueError):
        validate_request(**args)


def test_helper_uses_json_stdin_and_retains_definite_failure_candidates(monkeypatch):
    monkeypatch.setattr("webcodex.semantic._command", lambda: ["fixed-helper"])
    response = {
        "ok": False,
        "outcome": "not_started",
        "error_code": "ambiguous_element",
        "candidates": [{"name": "Save"}, {"name": "Save"}],
    }
    run = Mock(
        return_value=SimpleNamespace(stdout=json.dumps(response).encode(), stderr=b"", returncode=0)
    )
    monkeypatch.setattr("webcodex.semantic.subprocess.run", run)
    value = "$(do-not-run); 中文\nline 2"
    req = validate_request("set_text", {"pid": 1}, {"name": "Title"}, value)
    assert run_request(req) == response
    assert run.call_args.args[0] == ["fixed-helper"]
    assert json.loads(run.call_args.kwargs["input"])["text"] == value
    assert "shell" not in run.call_args.kwargs


def test_helper_timeout_or_response_loss_is_unknown_for_actions(monkeypatch):
    monkeypatch.setattr("webcodex.semantic._command", lambda: ["fixed-helper"])
    run = Mock(side_effect=subprocess.TimeoutExpired("helper", 20))
    monkeypatch.setattr("webcodex.semantic.subprocess.run", run)
    with pytest.raises(OutcomeUnknown):
        run_request(request())
    with pytest.raises(ValueError, match="timed out"):
        run_request(validate_request("windows"))
    run.side_effect = None
    run.return_value = SimpleNamespace(
        stdout=b"not json", stderr=b"private diagnostic", returncode=1
    )
    with pytest.raises(OutcomeUnknown) as caught:
        run_request(request())
    assert "private" not in str(caught.value)


def test_semantic_actions_share_desktop_lock_and_invalidate_coordinate_snapshot(monkeypatch):
    computer = Computer()
    computer.snapshot = ("old",)
    calls = []

    def dispatch(req):
        assert computer.lock.locked()
        calls.append(req)
        return {"ok": True}

    monkeypatch.setattr("webcodex.semantic.run_request", dispatch)
    assert computer.observe_ui()["ok"]
    assert computer.snapshot == ("old",)
    computer.control_ui("invoke", {"pid": 1}, {"name": "Save"})
    assert computer.snapshot is None
    assert [req["action"] for req in calls] == ["windows", "invoke"]
