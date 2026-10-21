"""Explicit opt-in: uses the Runner user's authenticated Codex CLI and model quota."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from test_harness import Harness, call

pytestmark = pytest.mark.skipif(
    os.environ.get("WEBCODEX_TEST_SUBAGENTS") != "1",
    reason="Set WEBCODEX_TEST_SUBAGENTS=1 to run real authenticated Codex workers",
)


async def test_real_mcp_parallel_codex_workers_read_and_write(tmp_path):
    harness = Harness(tmp_path)
    harness.runner_config.subagents_enabled = True
    harness.runner_config.max_subagents = 2
    (harness.repo / "seed.txt").write_text("WEB_CODEX_PARALLEL_FIXTURE_429\n", encoding="utf-8")
    await harness.start()
    evidence = {}
    try:
        async with harness.mcp() as client:
            task = await call(
                client, "task_create", {"project": "local/project", "title": "Real workers"}
            )
            args = {"task_id": task["id"]}
            read = await call(
                client,
                "subagent_spawn",
                {
                    **args,
                    "name": "Read fixture",
                    "request_id": "read-once",
                    "timeout_seconds": 300,
                    "prompt": (
                        "Read only seed.txt in the current working directory using a local tool. "
                        "Return exactly its one-line contents. Do not modify files. This is a small "
                        "integration test; no report, web access or other investigation is needed."
                    ),
                },
            )
            write = await call(
                client,
                "subagent_spawn",
                {
                    **args,
                    "name": "Write fixture",
                    "request_id": "write-once",
                    "write": True,
                    "timeout_seconds": 300,
                    "prompt": (
                        "Read seed.txt in the current working directory using a local tool, then "
                        "create only worker-result.txt containing seed.txt's exact contents. "
                        "Return exactly WROTE_FIXTURE after the file exists. This is a small integration "
                        "test; no report, web access or other investigation is needed."
                    ),
                },
            )
            ids = [read["result"]["agent_id"], write["result"]["agent_id"]]
            deadline = time.monotonic() + 310
            agents = []
            while time.monotonic() < deadline:
                observation = await call(
                    client,
                    "subagent_wait",
                    {
                        **args,
                        "agent_ids": ids,
                        "wait_seconds": 20,
                    },
                )
                agents = observation["result"]["agents"]
                pending = [agent["agent_id"] for agent in agents if agent["status"] == "running"]
                if not pending:
                    break
                # Wait on remaining workers when a completed worker would return immediately.
                for agent_id in pending:
                    await call(
                        client,
                        "subagent_get",
                        {
                            **args,
                            "agent_id": agent_id,
                            "wait_seconds": 20,
                        },
                    )
            evidence = {"agents": agents, "project": str(harness.repo)}
            assert all(agent["status"] == "completed" for agent in agents), agents
            assert "WEB_CODEX_PARALLEL_FIXTURE_429" in agents[0]["final"]
            assert "WROTE_FIXTURE" in agents[1]["final"]
            assert (harness.repo / "worker-result.txt").read_bytes() == (
                harness.repo / "seed.txt"
            ).read_bytes()
            assert max(agent["created"] for agent in agents) < min(
                agent["finished"] for agent in agents
            )
            listing = await call(client, "subagent_list", args)
            assert {agent["agent_id"] for agent in listing["result"]["agents"]} == set(ids)
    finally:
        if destination := os.environ.get("WEBCODEX_TEST_SUBAGENTS_EVIDENCE"):
            await asyncio.to_thread(
                Path(destination).write_text, json.dumps(evidence, ensure_ascii=False, indent=2)
            )
        await harness.close()
