"""Keep the last real output visible through long quiet process intervals."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from webcodex.management import Management
from webcodex.store import Store


class ProgressService:
    def __init__(self, store):
        self.store = store
        self.config = SimpleNamespace(clients={"owner": SimpleNamespace(permissions={"read"})})

    def check_project(self, owner, project, kind):
        assert (owner, project, kind) == ("owner", "local/project", "coding")

    def task_progress(self, task, *, include_output):
        activity = self.store.task_activity(task, include_output=include_output)
        work = activity.pop("work")
        return {
            **activity,
            "runner_online": True,
            "state": "observing",
            "recent_issues": [],
            "liveness": {"work": work},
        }


def test_management_preserves_last_output_after_thirteen_empty_observations(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("local", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "local/project", "Quiet long command")
        original_output = "開始訓練\nLoaded model; waiting for the next result.\n"
        cursor = len(original_output.encode("utf-8"))

        def observation(index, output, status, exit_code=None):
            job = store.enqueue(
                task, "exec_command" if index == 0 else "read_process", {}, str(index), 100
            )
            store.claim("local", "instance", 1)
            store.complete(
                "local",
                job["job_id"],
                "instance",
                "succeeded",
                {
                    "process_id": "quiet-process",
                    "status": status,
                    "exit_code": exit_code,
                    "output": output,
                    "cursor": 0 if index == 0 else cursor,
                    "next_cursor": cursor,
                },
            )
            with store.db:
                store.db.execute(
                    "UPDATE jobs SET updated=? WHERE id=?", (1000 + index, job["job_id"])
                )

        observation(0, original_output, "running")
        for index in range(1, 14):
            observation(index, "", "running")
        management = Management(ProgressService(store))
        request = SimpleNamespace(
            state=SimpleNamespace(owner="owner"), path_params={"task_id": task["id"]}
        )
        statements = []
        store.db.set_trace_callback(statements.append)
        running = json.loads(asyncio.run(management.detail(request)).body)
        store.db.set_trace_callback(None)
        output_query = next(sql for sql in statements if "SELECT result,updated FROM jobs" in sql)
        plan = store.db.execute("EXPLAIN QUERY PLAN " + output_query).fetchall()
        assert any(
            "process_observations" in row[3] and "task_id=? AND process_id=?" in row[3]
            for row in plan
        )
        assert running["outputs"][0]["status"] == "running"
        assert running["outputs"][0]["output"] == original_output
        observation(14, "", "completed", 0)
        completed = json.loads(asyncio.run(management.detail(request)).body)
        assert completed["outputs"][0]["status"] == "completed"
        assert completed["outputs"][0]["exit_code"] == 0
        assert completed["outputs"][0]["output"] == original_output
        assert (
            store.db.execute("SELECT count(*) FROM jobs WHERE tool='exec_command'").fetchone()[0]
            == 1
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([(0, 2, "AB"), (4, 6, "EF"), (2, 4, "CD")], "ABCDEF"),
        ([(0, 2, "�A"), (2, 3, "B")], "�AB"),
        ([(0, 4, "繁A"), (3, 8, "A體B")], "繁A體B"),
        ([(0, 4, "�ABC"), (2, 5, "BCD")], "�ABCD"),
    ],
)
def test_management_combines_saved_output_by_raw_cursor(tmp_path, pages, expected):
    store = Store(tmp_path)
    try:
        store.register("local", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "local/project", "Paged output")
        for start, stop, output in pages:
            job = store.enqueue(task, "read_process", {}, None, 10)
            store.claim("local", "instance", 1)
            store.complete(
                "local",
                job["job_id"],
                "instance",
                "succeeded",
                {
                    "process_id": "paged-process",
                    "status": "completed",
                    "output": output,
                    "cursor": start,
                    "next_cursor": stop,
                },
            )
        detail = Management(ProgressService(store)).detail_data("owner", task["id"])
        assert detail["outputs"][0]["output"] == expected
    finally:
        store.close()
