"""Regression coverage for Windows SQLite shared-memory cleanup after a smoke run."""

import runpy
import tempfile
from pathlib import Path


def test_installation_cleanup_retries_transient_windows_file_lock(monkeypatch, tmp_path):
    smoke = runpy.run_path(str(Path(__file__).parent / "fixtures" / "installation_smoke.py"))
    temporary_directory = smoke["RetryTemporaryDirectory"]
    original_cleanup = tempfile.TemporaryDirectory.cleanup
    attempts = 0

    def transient_failure(instance):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("SQLite shared-memory file is still mapped")
        return original_cleanup(instance)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", transient_failure)
    with temporary_directory(dir=tmp_path) as name:
        Path(name, "probe.txt").write_text("probe", encoding="utf-8")

    assert attempts == 3
    assert not Path(name).exists()
