from __future__ import annotations

import os
import subprocess
import sys

import pytest

from webcodex.config import Client, RunnerConfig, ServerConfig, server_url
from webcodex.extensions import copy_bundle, skill_metadata
from webcodex.filesystem import Workspace, sha256
from webcodex.processes import Processes
from webcodex.server import Service
from webcodex.store import Store


def directory_link(link, target):
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def test_guarded_files_and_directory_boundaries(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace(root)
    original = workspace.write("nested/file.txt", b"original\n", None)
    with pytest.raises(ValueError, match="changed"):
        workspace.write("nested/file.txt", b"lost update", None)
    workspace.write("nested/file.txt", b"revised\n", original["sha256"])
    with pytest.raises(ValueError, match="changed"):
        workspace.delete("nested/file.txt", original["sha256"])
    assert workspace.read("nested/file.txt") == b"revised\n"
    outside = tmp_path / "private.txt"
    outside.write_text("outside", encoding="utf-8", newline="\n")
    directory_link(root / "escape", tmp_path)
    if os.name != "nt":
        (root / "linked.txt").symlink_to(outside)
    os.link(outside, root / "hardlink.txt")
    for path in [
        "../private.txt",
        str(outside),
        ".git/config",
        "escape/private.txt",
        "linked.txt",
        "hardlink.txt",
    ]:
        with pytest.raises((ValueError, OSError)):
            workspace.read(path)
        with pytest.raises((ValueError, OSError)):
            workspace.write(path, b"overwrite", sha256(b"outside"))
    assert outside.read_text(encoding="utf-8") == "outside"
    if os.name != "nt":
        os.mkfifo(root / "pipe")
        with pytest.raises(ValueError, match="regular"):
            workspace.read("pipe")
    with pytest.raises(ValueError, match="links"):
        copy_bundle(root, tmp_path / "bundle")


def test_durable_replay_lost_claim_and_project_binding(tmp_path):
    store = Store(tmp_path)
    service_identity = store.service
    store.register("runner", "instance", {"project": "a" * 64})
    task = store.create_task("owner", "runner/project", "durability")
    job = store.enqueue(task, "write_file", {"content": "one"}, "request-1", 10)
    claimed = store.claim("runner", "instance", 1)
    assert claimed[0]["id"] == job["job_id"]
    # Publishing a new project can race with a claim response still in transit.
    store.register("runner", "instance", {"project": "a" * 64}, [], refresh_inventory=True)
    assert store.job(job["job_id"], "owner")["status"] == "running"
    # Claim response was lost: no replay, even when the same Runner reconnects.
    store.register("runner", "instance", {"project": "a" * 64}, [])
    assert store.job(job["job_id"], "owner")["status"] == "unknown"
    assert store.claim("runner", "instance", 1) == []
    store.close()
    store = Store(tmp_path)
    assert store.service == service_identity
    replay = store.enqueue(task, "write_file", {"content": "one"}, "request-1", 10)
    assert replay["job_id"] == job["job_id"]
    with pytest.raises(ValueError, match="different"):
        store.enqueue(task, "write_file", {"content": "two"}, "request-1", 10)
    store.complete("runner", job["job_id"], "instance", "succeeded", {"sha256": "evidence"})
    store.complete("runner", job["job_id"], "instance", "failed", {"error": "late duplicate"})
    assert store.job(job["job_id"], "owner")["result"] == {"sha256": "evidence"}
    with pytest.raises(ValueError, match="not found"):
        store.job(job["job_id"], "different-owner")
    store.register("runner", "instance", {"project": "b" * 64})
    with pytest.raises(ValueError, match="root changed"):
        store.enqueue(task, "write_file", {}, "new-request", 10)
    store.close()


def test_live_runner_claims_are_not_retargeted(tmp_path):
    store = Store(tmp_path)
    store.register("runner", "one", {"project": "a" * 64})
    task = store.create_task("owner", "runner/project", "owner")
    job = store.enqueue(task, "exec_command", {}, None, 10)
    store.claim("runner", "one", 1)
    with pytest.raises(ValueError, match="still connected"):
        store.register("runner", "two", {"project": "a" * 64})
    with pytest.raises(ValueError, match="instance"):
        store.register("runner", "two", {"project": "a" * 64}, refresh_inventory=True)
    store.register("runner", "one", {"project": "a" * 64}, [job["job_id"]])
    assert store.job(job["job_id"], "owner")["status"] == "running"
    with pytest.raises(ValueError, match="instance"):
        store.complete("runner", job["job_id"], "two", "succeeded", {})
    store.close()


async def test_process_output_limit_timeout_cancellation_and_scope(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": project},
        max_output_bytes=4096,
    )
    processes = Processes(config)
    try:
        await processes.start(
            "output", "task-a", [sys.executable, "-c", "print('x' * 100000)"], project, 5
        )
        await processes.wait("output", "task-a", 6)
        result = processes.read("output", "task-a")
        assert result["status"] == "completed" and result["truncated"]
        assert len(result["output"]) == 4096 and result["total_bytes"] > 100000
        with pytest.raises(ValueError, match="not found"):
            processes.read("output", "task-b")
        await processes.start(
            "timeout", "task-a", [sys.executable, "-c", "import time; time.sleep(60)"], project, 1
        )
        await processes.wait("timeout", "task-a", 3)
        assert processes.read("timeout", "task-a")["status"] == "timed_out"
        await processes.start(
            "cancel", "task-a", [sys.executable, "-c", "import time; time.sleep(60)"], project, 60
        )
        await processes.cancel("cancel", "task-a")
        assert processes.read("cancel", "task-a")["status"] == "cancelled"
        await processes.start(
            "grandchild",
            "task-a",
            [
                sys.executable,
                "-c",
                "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('parent-done', end='')",
            ],
            project,
            5,
        )
        await processes.wait("grandchild", "task-a", 5)
        assert processes.read("grandchild", "task-a")["status"] == "completed"
        assert processes.read("grandchild", "task-a")["output"] == "parent-done"
    finally:
        await processes.close()
    restored = Processes(config)
    assert restored.read("output", "task-a")["truncated"]
    await restored.close()


def test_configuration_rejects_unencrypted_remote_and_exposed_state(tmp_path):
    common = {"runner_id": "local", "token": "r" * 40, "projects": {"project": tmp_path}}
    with pytest.raises(ValueError, match="HTTPS"):
        RunnerConfig(
            **common, server_url="http://remote.example:8765", state_dir=tmp_path.parent / "state"
        )
    with pytest.raises(ValueError, match="outside"):
        RunnerConfig(**common, server_url="http://127.0.0.1:8765", state_dir=tmp_path / "state")
    for url in [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com/mcp",
    ]:
        with pytest.raises(ValueError):
            server_url(url)


def test_dispatch_rechecks_revoked_permissions(tmp_path):
    config = ServerConfig(
        state_dir=tmp_path,
        admin_token="a" * 40,
        clients={
            "owner": Client(token="c" * 40, projects=["runner/project"], approval_required=set())
        },
        runners={"runner": "r" * 40},
    )
    service = Service(config)
    service.store.register("runner", "instance", {"project": "a" * 64})
    task = service.store.create_task("owner", "runner/project", "policy")
    first = service.store.enqueue(task, "exec_command", {}, None, 10)
    config.clients["owner"].permissions.remove("execute")
    assert service.claim("runner", "instance", 1) == []
    assert service.store.job(first["job_id"], "owner")["status"] == "failed"
    config.clients["owner"].permissions.add("execute")
    second = service.store.enqueue(task, "exec_command", {}, None, 10)
    assert service.claim("runner", "instance", 1)[0]["id"] == second["job_id"]
    service.store.close()


def test_skill_frontmatter_and_root_replacement(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "SKILL.md").write_text(
        '---\nname: example\ndescription: "Inspect --- then report."\n---\n# 流程\n',
        encoding="utf-8",
        newline="\n",
    )
    assert skill_metadata(Workspace(root))["description"] == "Inspect --- then report."
    config = RunnerConfig(
        server_url="http://127.0.0.1:8765",
        runner_id="local",
        token="r" * 40,
        state_dir=tmp_path / "state",
        projects={"project": root},
    )
    old_binding = config.inventory()
    root.rename(tmp_path / "original")
    root.mkdir()
    assert config.inventory() != old_binding
    root.rmdir()
    directory_link(root, tmp_path / "original")
    with pytest.raises(ValueError, match="replaced"):
        config.inventory()
