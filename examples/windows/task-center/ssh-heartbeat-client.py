"""Keep the task-center SSH connection alive and retry after transport failure."""

import contextlib
import errno
import os
import subprocess
import sys
import time
from pathlib import Path

HEARTBEAT_SECONDS = 10
RETRY_SECONDS = 5


def run_session(argv, log):
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options = {
            "startupinfo": startup,
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        bufsize=0,
        stdout=subprocess.DEVNULL,
        stderr=log,
        **options,
    )
    try:
        while process.poll() is None:
            try:
                process.stdin.write(b"alive\n")
            except OSError as error:
                # Windows reports a closed anonymous pipe as EINVAL.
                if error.errno != errno.EPIPE and not (
                    os.name == "nt" and error.errno == errno.EINVAL
                ):
                    raise
                break
            try:
                return process.wait(timeout=HEARTBEAT_SECONDS)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
        process.stdin.close()
    return process.returncode


def keep_connected(argv, log):
    while True:
        code = run_session(argv, log)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        log.write(f"{stamp} SSH exited {code}; retrying in {RETRY_SECONDS}s\n".encode())
        log.flush()
        time.sleep(RETRY_SECONDS)


@contextlib.contextmanager
def instance_lock(path):
    """Hold one launcher across scheduler sessions, including the reconnect gap."""
    import msvcrt

    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in (13, 36):
                raise
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def ssh_command(arguments):
    """Choose the client explicitly; all remaining arguments belong to SSH."""
    if arguments[:1] == ["--ssh-executable"]:
        if len(arguments) < 2:
            raise SystemExit("--ssh-executable requires a path")
        return arguments[1:]
    ssh = Path(os.environ["SystemRoot"]) / "System32/OpenSSH/ssh.exe"
    return [str(ssh), *arguments]


def main():
    directory = Path(__file__).parent
    with instance_lock(directory / "ssh-heartbeat-client.lock") as acquired:
        if not acquired:
            return 0
        with (directory / "heartbeat-client.log").open("ab") as log:
            keep_connected(ssh_command(sys.argv[1:]), log)


if __name__ == "__main__":
    raise SystemExit(main())
