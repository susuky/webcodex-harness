"""Run the existing startup check without creating a terminal window."""

import os
import subprocess
import sys
from pathlib import Path


def main(config_dir: Path) -> int:
    log_dir = config_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    with (log_dir / "keepalive-launcher.log").open("ab") as log:
        return subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(config_dir / "start-windows-webcodex.ps1"),
            ],
            cwd=config_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            startupinfo=startup,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve(strict=True)))
