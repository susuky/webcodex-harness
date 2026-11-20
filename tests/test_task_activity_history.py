from __future__ import annotations

import asyncio
import json

from webcodex.store import Store

pytest_plugins = ["test_harness"]


def add_history(store, task):
    rows = []

    def observation(process, status, created, updated, *, job_status="succeeded"):
        job_id = f"{len(rows) + 1:032x}"
        result = {"process_id": process, "status": status, "exit_code": None}
        rows.append(
            (
                job_id,
                task["id"],
                task["owner"],
                task["runner"],
                task["project"],
                task["binding"],
                job_status,
                json.dumps(result),
                process,
                status,
                created,
                updated,
            )
        )
        return job_id

    for index in range(20000):
        observation("finished-history", "completed", index, index)
    observation("old-live", "running", 1, 30)
    for index in range(110):
        observation(f"new-{index}", "completed", 20000 + index, 20000 + index)
    # Keep terminal and running projections stable even at equal observation times.
    observation("tie-finished", "running", 2, 50)
    observation("tie-finished", "completed", 3, 50)
    observation("tie-live", "running", 4, 60)
    observation("tie-live", "running", 5, 60)
    active_agent = observation("legacy-live", "running", 6, 10)
    observation("legacy-finished", "running", 7, 10)
    observation("uncertain", "running", 8, 80, job_status="unknown")
    with store.db:
        store.db.executemany(
            "INSERT INTO jobs(id,task_id,owner,runner,project,binding,tool,arguments,status,result,"
            "process_id,process_status,created,updated) "
            "VALUES (?,?,?,?,?,?,'read_process','{}',?,?,?,?,?,?)",
            rows,
        )
        for row in rows:
            if row[6] == "succeeded":
                store.observe_process(
                    task["id"], row[8], row[0], "read_process", row[9], None, row[10], row[11]
                )
        store.db.executemany(
            "UPDATE process_states SET tool='subagent_get',status=?,exit_code=?,"
            "started_at=?,observed_at=? WHERE task_id=? AND process_id=?",
            [
                ("running", None, 6, 20, task["id"], "legacy-live"),
                ("completed", 0, 7, 21, task["id"], "legacy-finished"),
            ],
        )
        store.db.execute(
            "UPDATE jobs SET tool='subagent_spawn',result=? WHERE id=?",
            (
                json.dumps(
                    {"agent_id": "legacy-live", "process_id": "legacy-live", "status": "running"}
                ),
                active_agent,
            ),
        )


def test_pending_activity_counts_only_unfinished_jobs_and_keeps_truncation_exact(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "Pending over history")
        other = store.create_task("owner", "runner/project", "Another task")
        foreign = store.create_task("another", "runner/project", "Another owner")
        with store.db:
            store.db.executemany(
                "INSERT INTO jobs(id,task_id,owner,runner,project,binding,tool,arguments,status,"
                "created,updated) VALUES (?,?,?,'runner','project',?,'read_file','{}',?,1,1)",
                (
                    (
                        f"{index:032x}",
                        selected["id"],
                        selected["owner"],
                        selected["binding"],
                        status,
                    )
                    for index, (selected, status) in enumerate(
                        [(task, "succeeded")] * 20000
                        + [
                            (task, ("queued", "running", "pending_approval")[index % 3])
                            for index in range(103)
                        ]
                        + [(other, "queued")] * 200
                        + [(foreign, "queued")] * 200
                    )
                ),
            )
        # The existing partial index bounds this work to pending rows. Counting
        # the 20,000 completed jobs exhausts the same SQLite instruction budget.
        store.db.set_progress_handler(lambda: 1, 20000)
        try:
            activity = store.task_activity(task, include_output=False)
        finally:
            store.db.set_progress_handler(None, 0)
        assert "job_counts" not in activity
        assert activity["work"]["pending_count"] == 103
        assert len(activity["pending_jobs"]) == 100
        assert activity["pending_jobs_truncated"]
        assert {job["task_id"] for job in activity["pending_jobs"]} == {task["id"]}
        assert {job["status"] for job in activity["pending_jobs"]} == {
            "queued",
            "running",
            "pending_approval",
        }
    finally:
        store.close()


def test_history_queries_keep_all_live_processes_without_ranking_every_observation(tmp_path):
    store = Store(tmp_path)
    try:
        store.register("runner", "instance", {"project": "b" * 64})
        task = store.create_task("owner", "runner/project", "History")
        add_history(store, task)
        queries = []
        store.db.set_trace_callback(queries.append)
        try:
            activity = store.task_activity(task, include_output=False)
            running = store.running_processes(task["id"])
        finally:
            store.db.set_trace_callback(None)
        assert "job_counts" not in activity
        assert not any("GROUP BY status" in query for query in queries)
        assert len(activity["process_observations"]) == 100
        assert all(item["status"] == "completed" for item in activity["process_observations"])
        assert activity["work"] == {
            "running_count": 3,
            "pending_count": 0,
            "observed_at": 20.0,
        }
        assert {
            (
                item["process_id"],
                item["status"],
                item["started_at"],
                item["observed_at"],
                item["tool"],
            )
            for item in running
        } == {
            ("old-live", "running", 1.0, 30.0, "read_process"),
            ("tie-live", "running", 4.0, 60.0, "read_process"),
            ("legacy-live", "running", 6.0, 20.0, "subagent_get"),
        }
        # Current process queries stay bounded even after 20,000 observations.
        for query in queries:
            if "FROM process_states" in query:
                store.db.set_progress_handler(lambda: 1, 20000)
                try:
                    store.db.execute(query).fetchall()
                finally:
                    store.db.set_progress_handler(None, 0)
    finally:
        store.close()


async def test_large_history_reads_keep_runner_and_http_results_available(harness):
    store = harness.app.state.service.store
    task = store.create_task("owner", "local/project", "History over HTTP")
    add_history(store, task)
    instance = harness.runner.instance
    async with harness.http() as client:
        listing, health = await asyncio.gather(
            client.get("/api/manage/state"), client.get("/healthz")
        )
        assert listing.status_code == health.status_code == 200
        listed = listing.json()["tasks"][0]
        assert listed["liveness"]["work"]["running_count"] == 3
        assert {
            item["process_id"] for item in listed["processes"] if item["status"] == "running"
        } == {
            "old-live",
            "tie-live",
            "legacy-live",
        }
        assert listed["runner_online"]
        result = await client.post(
            "/api/call", json={"name": "task_get", "arguments": {"task_id": task["id"]}}
        )
        assert result.status_code == 200
        assert "job_counts" not in result.json()["progress"]
        assert result.json()["progress"]["liveness"]["work"] == {
            "running_count": 3,
            "pending_count": 0,
            "observed_at": 20.0,
        }
        presented = await client.post(
            "/api/call", json={"name": "task_present", "arguments": {"task_id": task["id"]}}
        )
        assert presented.status_code == 200
        assert {
            action["arguments"]["process_id"]
            for action in presented.json()["handoff"]["next_actions"]
            if action["tool"] == "read_process"
        } == {"old-live", "tie-live", "legacy-live"}
        observed = await client.post(
            "/api/call",
            json={"name": "job_get", "arguments": {"job_id": f"{1:032x}"}},
        )
        assert observed.status_code == 200
        assert observed.json()["result"]["process_id"] == "finished-history"
        assert observed.json()["status"] == "succeeded"
    assert store.one("SELECT instance FROM runners WHERE id='local'")["instance"] == instance
