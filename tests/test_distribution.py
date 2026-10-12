"""Run against actual build outputs with WEBCODEX_TEST_DIST=<directory>."""

import os
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.mark.skipif(not os.environ.get("WEBCODEX_TEST_DIST"), reason="Requires built distributions")
def test_distribution_contains_sources_and_excludes_private_files():
    directory = Path(os.environ["WEBCODEX_TEST_DIST"])
    archives = list(directory.glob("*.tar.gz")) + list(directory.glob("*.whl"))
    assert any(path.suffix == ".whl" for path in archives)
    assert any(path.name.endswith(".tar.gz") for path in archives)
    for path in archives:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
            assert "webcodex/task_app.html" in names
        else:
            with tarfile.open(path) as archive:
                names = [member.name for member in archive.getmembers()]
            assert any(name.endswith("/src/webcodex/task_app.html") for name in names)
            assert any(name.endswith("/tests/test_security_regressions.py") for name in names)
            assert any(name.endswith("/pyproject.toml") for name in names)
        assert not any(".codex-remote-attachments" in Path(name).parts for name in names)
        assert not any(
            Path(name).name in {".env", "server.json", "runner.json", "client.json"}
            for name in names
        )
