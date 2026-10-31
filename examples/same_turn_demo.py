"""Run a real command once and collect its result through the existing Server."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from webcodex.cli import DEFAULT_DIR
from webcodex.config import server_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_DIR / "client.json")
    args = parser.parse_args()
    config = json.loads(args.config.expanduser().read_text(encoding="utf-8-sig"))
    url = server_url(config["server_url"]) + "/api/call"

    def call(name, arguments):
        request = urllib.request.Request(
            url,
            data=json.dumps({"name": name, "arguments": arguments}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config["token"],
            },
        )
        with urllib.request.urlopen(request, timeout=35) as response:
            return json.load(response)

    def result(job):
        deadline = time.monotonic() + 60
        while job.get("status") in {"queued", "running"}:
            if time.monotonic() > deadline:
                raise RuntimeError("Read the saved job with job_get: " + job["job_id"])
            job = call("job_get", {"job_id": job["job_id"], "wait_seconds": 1})
        if job.get("status") != "succeeded":
            raise RuntimeError(
                "Inspect job " + job.get("job_id", "") + ": " + job.get("status", "unknown")
            )
        return job["result"]

    task = call(
        "task_create",
        {
            "project": args.project,
            "title": "同回合命令示範",
            "request_id": "demo-" + uuid.uuid4().hex,
        },
    )
    print("task_id:", task["id"], flush=True)
    process = result(
        call(
            "exec_command",
            {
                "task_id": task["id"],
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('demo started'); time.sleep(4); print('demo finished')",
                ],
                "yield_time_ms": 0,
                "request_id": "demo-command",
            },
        )
    )
    print("process_id:", process["process_id"], flush=True)
    sys.stdout.write(process.get("output", ""))
    while process["status"] == "running" or process.get("has_more"):
        process = result(
            call(
                "read_process",
                {
                    "task_id": task["id"],
                    "process_id": process["process_id"],
                    "cursor": process.get("next_cursor", 0),
                    "wait_seconds": 1,
                },
            )
        )
        sys.stdout.write(process.get("output", ""))
    print("exit_code:", process["exit_code"])
    call(
        "task_update",
        {
            "task_id": task["id"],
            "status": "completed",
            "summary": "示範命令完成，exit_code=" + str(process["exit_code"]),
            "plan": [{"step": "同一命令執行並讀回結果", "status": "completed"}],
            "handoff": {
                "changed_files": [],
                "validation": ["demo exit_code=" + str(process["exit_code"])],
                "issues": [],
                "next_steps": [],
            },
        },
    )
    if process["exit_code"]:
        raise SystemExit(process["exit_code"])


if __name__ == "__main__":
    main()
