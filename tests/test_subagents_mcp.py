from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest
from test_harness import Harness, call

import webcodex.subagents as subagent_module

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Executable Unix fixture CLI")


async def test_mcp_parallel_workers_results_dedupe_and_app_observation(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,time\n"
        "request=json.loads(sys.stdin.read())\n"
        "time.sleep(request.get('delay',0))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':request['final']}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(subagent_module, "executable", lambda name, config: str(binary))
    harness = Harness(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
    harness.runner_config.environment["CODEX_HOME"] = str(codex_home)
    harness.runner_config.subagents_enabled = True
    await harness.start()
    try:
        async with harness.mcp() as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "subagent_spawn",
                "subagent_get",
                "subagent_wait",
                "subagent_list",
                "subagent_cancel",
            } <= tools
            task = await call(
                client, "task_create", {"project": "local/project", "title": "Parallel"}
            )
            args = {"task_id": task["id"]}
            first_args = {
                **args,
                "name": "First",
                "prompt": json.dumps({"delay": 1, "final": "甲"}),
                "request_id": "first",
            }
            first = await call(client, "subagent_spawn", first_args)
            duplicate = await call(client, "subagent_spawn", first_args)
            assert duplicate["job_id"] == first["job_id"]
            assert first["next_action"]["tool"] == "subagent_get"
            second = await call(
                client,
                "subagent_spawn",
                {
                    **args,
                    "name": "Second",
                    "prompt": json.dumps({"delay": 1, "final": "乙"}),
                    "request_id": "second",
                },
            )
            ids = [first["result"]["agent_id"], second["result"]["agent_id"]]
            assert len(harness.runner.processes.active) == 2
            service = harness.app.state.service
            current = await call(client, "task_get", args)
            assert current["progress"]["state"] == "observing"
            assert {
                action["arguments"]["agent_id"]
                for action in current["progress"]["next_actions"]
                if action["tool"] == "subagent_get"
            } == set(ids)
            meta = {"openai/session": "subagent-test-window"}
            bound = await client.call_tool(
                "task_app", {**args, "view_id": "1" * 32, "action": "bind"}, meta=meta
            )
            assert not bound.isError
            waiting = await client.call_tool(
                "task_wait",
                {**args, "job_ids": [first["job_id"], second["job_id"]], "request_id": "observe"},
                meta=meta,
            )
            assert not waiting.isError
            deadline = time.monotonic() + 15
            while service.continuations.current(task["id"])["state"] != "ready":
                await service.refresh_task_wait(service.store.task(task["id"], "owner"))
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            observed = [
                job
                for job in service.store.rows(
                    "SELECT tool FROM jobs WHERE task_id=?", (task["id"],)
                )
                if job["tool"] == "subagent_get"
            ]
            assert observed  # The App observes structured worker results, not raw CLI process logs.
            results = await call(
                client, "subagent_wait", {**args, "agent_ids": ids, "wait_seconds": 0}
            )
            agents = results["result"]["agents"]
            assert {agent["final"] for agent in agents} == {"甲", "乙"}
            assert all(agent["status"] == "completed" for agent in agents)
            assert max(agent["created"] for agent in agents) < min(
                agent["finished"] for agent in agents
            )
            listing = await call(client, "subagent_list", args)
            assert len(listing["result"]["agents"]) == 2
            other = await call(
                client, "task_create", {"project": "local/project", "title": "Other"}
            )
            await call(
                client, "subagent_get", {"task_id": other["id"], "agent_id": ids[0]}, error=True
            )
            done = await call(client, "subagent_get", {**args, "agent_id": ids[0]})
            assert done["outcome"] == "completed" and "next_action" not in done
    finally:
        await harness.close()
