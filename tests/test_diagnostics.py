from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from webcodex import diagnostics as module
from webcodex.bridge import _trace_request
from webcodex.diagnostics import Diagnostics, hash_id


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_safe_request_metadata_excludes_payloads_and_invalid_fields(tmp_path):
    sentinel = "PRIVATE_REQUEST_PAYLOAD/token/argv/file/output"
    diagnostics = Diagnostics("server", tmp_path)
    diagnostics.record(
        "tool.completed",
        trace_id="1" * 32,
        http_id="2" * 32,
        rpc_id_hash=hash_id(sentinel),
        tool="exec_command",
        task_id="3" * 32,
        job_id="4" * 32,
        process_outcome="failed",
        exit_code=7,
        is_error=False,
        duration_ms=12.5,
        arguments=sentinel,
        result=sentinel,
        authorization=sentinel,
        exception_message=sentinel,
        resource_hash=sentinel,
        window_hash=sentinel,
        wait_id=sentinel,
        owner_hash=sentinel,
        action=sentinel,
        outcome=sentinel,
        response_bytes=float("inf"),
        http_status=999,
    )
    diagnostics.record(sentinel, tool=sentinel)
    diagnostics.close()
    path = tmp_path / "server.jsonl"
    text = path.read_text(encoding="utf-8")
    assert sentinel not in text
    (row,) = records(path)
    assert set(row) == {
        "time",
        "component",
        "event",
        "trace_id",
        "http_id",
        "rpc_id_hash",
        "tool",
        "task_id",
        "job_id",
        "process_outcome",
        "exit_code",
        "is_error",
        "duration_ms",
    }
    assert row["process_outcome"] == "failed" and row["exit_code"] == 7
    assert row["rpc_id_hash"] == hash_id(sentinel)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


def test_bridge_trace_records_success_error_and_cancellation_without_error_message(tmp_path):
    diagnostics = Diagnostics("bridge", tmp_path)
    with _trace_request(diagnostics, "tool", "sensitive-rpc-id", tool="task_get") as result:
        result["is_error"] = False
        result["outcome"] = "returned"
    with pytest.raises(ValueError, match="PRIVATE_PAYLOAD"):
        with _trace_request(diagnostics, "tool", 8, tool="task_get"):
            raise ValueError("PRIVATE_PAYLOAD")
    with pytest.raises(asyncio.CancelledError):
        with _trace_request(diagnostics, "tool", 9, tool="job_get"):
            raise asyncio.CancelledError("PRIVATE_PAYLOAD")
    diagnostics.close()
    path = tmp_path / "bridge.jsonl"
    assert "PRIVATE_PAYLOAD" not in path.read_text(encoding="utf-8")
    assert "sensitive-rpc-id" not in path.read_text(encoding="utf-8")
    rows = records(path)
    assert [row["event"] for row in rows] == [
        "tool.started",
        "tool.completed",
        "tool.started",
        "tool.error",
        "tool.started",
        "tool.cancelled",
    ]
    for start, finish in zip(rows[::2], rows[1::2], strict=True):
        assert start["trace_id"] == finish["trace_id"]
        assert start["rpc_id_hash"] == finish["rpc_id_hash"]
        assert finish["duration_ms"] >= 0
    assert len({row["trace_id"] for row in rows}) == 3
    assert rows[3]["exception_type"] == "ValueError"
    assert rows[1]["is_error"] is False


def test_rotation_has_four_private_bounded_files(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_BYTES", 1024)
    diagnostics = Diagnostics("server", tmp_path)
    for _ in range(100):
        diagnostics.record("tool.completed", trace_id="a" * 32, tool="projects_list")
    diagnostics.close()
    paths = list(tmp_path.glob("server.jsonl*"))
    assert len(paths) == 4
    assert all(path.stat().st_size <= 1024 for path in paths)
    if os.name != "nt":
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    assert all(records(path) for path in paths)


def test_stderr_logging_and_write_failure_preserve_original_operation(
    tmp_path, capsys, monkeypatch
):
    diagnostics = Diagnostics("bridge")
    diagnostics.record("tool.started", tool="projects_list")
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["event"] == "tool.started"

    def fail(*args, **kwargs):
        raise OSError("PRIVATE_FILESYSTEM_ERROR")

    monkeypatch.setattr(diagnostics.logger, "info", fail)
    with _trace_request(diagnostics, "tool", 1, tool="projects_list"):
        pass
    with pytest.raises(ValueError, match="original"):
        with _trace_request(diagnostics, "tool", 2, tool="projects_list"):
            raise ValueError("original")
    diagnostics.close()
    monkeypatch.setattr(module, "private_dir", fail)
    fallback = Diagnostics("server", tmp_path)
    fallback.record("http.completed", method="POST", route="/mcp", http_status=200)
    fallback.close()
    output = capsys.readouterr()
    assert output.out == ""
    assert "PRIVATE_FILESYSTEM_ERROR" not in output.err
    assert json.loads(output.err)["http_status"] == 200


def test_hash_id_and_delivery_fields_are_bounded(tmp_path):
    assert hash_id(None) is None and hash_id(True) is None and hash_id({}) is None
    assert hash_id(42) == hash_id("42")
    assert len(hash_id("\ud800")) == 64
    diagnostics = Diagnostics("server", tmp_path)
    diagnostics.record(
        "tool.completed",
        tool="task_app",
        action="finish",
        delivery_error="rpc_error",
        delivery_error_code=-32601,
        delivery_outcome="unknown",
        wait_state="prepared",
        is_error=True,
    )
    diagnostics.record("tool.completed", delivery_error_code=2**40, exit_code=True)
    diagnostics.close()
    row, invalid = records(tmp_path / "server.jsonl")
    assert row["delivery_error_code"] == -32601 and row["is_error"] is True
    assert "delivery_error_code" not in invalid and "exit_code" not in invalid


def test_controller_and_handoff_metadata_exclude_unknown_reasons_and_view_identity(tmp_path):
    sentinel = "PRIVATE_VIEW_AND_REASON_f3b901"
    diagnostics = Diagnostics("server", tmp_path)
    diagnostics.record(
        "tool.completed",
        tool="task_app",
        control_bound=False,
        controller_generation=2**53 - 1,
        control_reason="replaced_view",
        handoff_state="blocked",
        handoff_reason="expired",
        view_id=sentinel,
        window=sentinel,
    )
    diagnostics.record(
        "tool.completed",
        control_bound=1,
        controller_generation=2**53,
        control_reason=sentinel,
        handoff_state=sentinel,
        handoff_reason=sentinel,
    )
    diagnostics.close()
    path = tmp_path / "server.jsonl"
    assert sentinel not in path.read_text(encoding="utf-8")
    valid, invalid = records(path)
    assert valid["control_bound"] is False
    assert valid["controller_generation"] == 2**53 - 1
    assert valid["control_reason"] == "replaced_view"
    assert valid["handoff_state"] == "blocked" and valid["handoff_reason"] == "expired"
    assert set(invalid) == {"time", "component", "event"}
