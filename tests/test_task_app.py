import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Task App regression requires Node.js")
def test_task_app_continuation_recovery():
    result = subprocess.run(
        [shutil.which("node"), "--test", str(Path(__file__).with_name("task_app.test.cjs"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
