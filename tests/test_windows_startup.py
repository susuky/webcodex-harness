"""Run the shipped WSL control helper against isolated native child processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")

PROBE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:WEBCODEX_TEST_SCRIPT, [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw $errors[0].Message }
$definition = $ast.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-WslControl'
}, $true)
. ([ScriptBlock]::Create($definition.Extent.Text))
$wslCommand = $env:WEBCODEX_TEST_PYTHON
try {
    Invoke-WslControl -Arguments @(
        $env:WEBCODEX_TEST_CHILD, $env:WEBCODEX_TEST_MODE, $env:WEBCODEX_TEST_PID
    ) -TimeoutMilliseconds 500
    exit 0
} catch {
    Write-Output $_.Exception.Message
    exit 7
}
"""


@pytest.mark.parametrize("mode", ["success", "failure", "timeout"])
def test_wsl_control_reports_failure_and_stops_its_timed_out_child(tmp_path, mode):
    child = tmp_path / "child with spaces.py"
    marker = tmp_path / "child.pid"
    child.write_text(
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "if sys.argv[1]=='timeout': time.sleep(30)\n"
        "if sys.argv[1]=='failure':\n"
        "    print('fake control failure',file=sys.stderr)\n"
        "    raise SystemExit(9)\n"
        "print('control completed')\n",
        encoding="utf-8",
    )
    source = Path(__file__).resolve().parents[1] / "examples/windows/wsl/start-wsl-runner.ps1"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", PROBE],
        env={
            **os.environ,
            "WEBCODEX_TEST_SCRIPT": str(source),
            "WEBCODEX_TEST_PYTHON": getattr(sys, "_base_executable", sys.executable),
            "WEBCODEX_TEST_CHILD": str(child),
            "WEBCODEX_TEST_MODE": mode,
            "WEBCODEX_TEST_PID": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if mode == "success":
        assert result.returncode == 0 and "control completed" in result.stdout, result.stderr
    elif mode == "failure":
        assert result.returncode == 7, result.stderr
        assert "failed (9): fake control failure" in result.stdout
    else:
        import pywintypes
        import win32api
        import win32con
        import win32event

        assert result.returncode == 7 and "timed out" in result.stdout, result.stderr
        pid = int(marker.read_text())
        try:
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        except pywintypes.error as error:
            assert error.winerror == 87  # Process has already been reaped.
        else:
            try:
                assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
            finally:
                handle.Close()
