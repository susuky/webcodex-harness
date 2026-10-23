from __future__ import annotations

from webcodex.store import Store


def test_legacy_worker_states_survive_raw_logs_stale_observations_and_restart(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "instance", {"project": "b" * 64})
    task = store.create_task("owner", "runner/project", "workers")

    def complete(tool, result):
        job = store.enqueue(task, tool, {}, None, 100)
        store.claim("runner", "instance", 1)
        store.complete("runner", job["job_id"], "instance", "succeeded", result)
        return job["job_id"]

    try:
        ids = []
        for name in ("one", "two"):
            job = store.enqueue(task, "subagent_spawn", {}, name, 100)
            store.claim("runner", "instance", 1)
            agent_id = job["job_id"]
            ids.append(agent_id)
            store.complete(
                "runner",
                agent_id,
                "instance",
                "succeeded",
                {
                    "agent_id": agent_id,
                    "process_id": agent_id,
                    "status": "running",
                },
            )
            assert store.job(agent_id, "owner")["next_action"]["tool"] == "read_process"
        complete(
            "subagent_wait",
            {
                "agents": [
                    {"agent_id": ids[0], "status": "completed", "exit_code": 0},
                    {"agent_id": ids[1], "status": "failed", "exit_code": 0},
                ],
                "timed_out": False,
            },
        )

        def states():
            return {
                p["process_id"]: p["status"]
                for p in store.task_activity(task)["process_observations"]
            }

        assert states() == {ids[0]: "completed", ids[1]: "failed"}
        # A diagnostic read reports the CLI exit, not successful model completion.
        complete("read_process", {"process_id": ids[1], "status": "completed", "exit_code": 0})
        assert states()[ids[1]] == "failed"
        # An earlier running observation may arrive after a terminal result.
        complete("subagent_list", {"agents": [{"agent_id": ids[0], "status": "running"}]})
        assert states()[ids[0]] == "completed"
        job = store.enqueue(task, "subagent_spawn", {}, "old-running", 100)
        store.claim("runner", "instance", 1)
        old_id = job["job_id"]
        store.complete(
            "runner",
            old_id,
            "instance",
            "succeeded",
            {"agent_id": old_id, "process_id": old_id, "status": "running"},
        )
        complete("read_process", {"process_id": old_id, "status": "interrupted"})
        assert states()[old_id] == "interrupted"
        store.close()
        store = Store(tmp_path)
        assert states() == {ids[0]: "completed", ids[1]: "failed", old_id: "interrupted"}
    finally:
        store.close()
