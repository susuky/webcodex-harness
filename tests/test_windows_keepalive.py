"""Exercise the GUI entry point and its real console subprocess on Windows."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires native Windows")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_hidden_keepalive_preserves_output_and_exit_code(tmp_path, exit_code):
    config = tmp_path / "中文 startup ' folder"
    config.mkdir()
    probe = config / "probe.py"
    probe.write_text(
        "import ctypes, json\n"
        "console = ctypes.windll.kernel32.GetConsoleWindow()\n"
        "print(json.dumps({'console': console}))\n",
        encoding="utf-8",
    )
    script = config / "start-windows-webcodex.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n"
        "& $env:WEBCODEX_TEST_PYTHON (Join-Path $PSScriptRoot 'probe.py')\n"
        f"exit {exit_code}\n",
        encoding="utf-8-sig",
    )
    launcher = Path(__file__).resolve().parents[1] / "examples/windows/start-hidden.py"
    result = subprocess.run(
        [str(Path(sys.executable).with_name("pythonw.exe")), str(launcher), str(config)],
        env={**os.environ, "WEBCODEX_TEST_PYTHON": sys.executable},
        timeout=30,
        check=False,
    )
    assert result.returncode == exit_code
    output = (config / "logs/keepalive-launcher.log").read_text(encoding="utf-8-sig")
    assert json.loads(output) == {"console": 0}
