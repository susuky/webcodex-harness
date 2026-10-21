from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import webcodex.runtime as runtime_module
from webcodex.runtime import Runtime


async def test_macos_structured_helpers_refuse_unpinned_cwd(tmp_path: Path, monkeypatch):
    runtime = object.__new__(Runtime)
    runtime.workspace = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(runtime_module.sys, "platform", "darwin")
    # Refuse before executable lookup or spawn: replacing the directory cannot redirect Git.
    with pytest.raises(ValueError, match="pinned working-directory"):
        await runtime._run(["git", "diff"], tmp_path)
