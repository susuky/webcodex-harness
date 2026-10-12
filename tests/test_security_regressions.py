from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from webcodex.catalog import OutcomeUnknown
from webcodex.config import RunnerConfig, project_binding
from webcodex.extensions import copy_bundle
from webcodex.filesystem import Workspace
from webcodex.processes import executable
from webcodex.projects import ProjectOperations, restore_projects
from webcodex.runner import Runner


def directory_link(link: Path, target: Path):
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
async def runner(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    instance = Runner(
        RunnerConfig(
            server_url="http://127.0.0.1:8765",
            runner_id="local",
            token="r" * 40,
            state_dir=tmp_path / "state",
            projects={"project": root},
        )
    )
    try:
        yield instance
    finally:
        await instance.processes.close()
        for extensions in instance.extensions.values():
            await extensions.close()
        instance.db.close()
        instance.lock_file.close()


def job(runner, tool, arguments, job_id="security-job"):
    return {
        "id": job_id,
        "instance": runner.instance,
        "task_id": "security-task",
        "project": "project",
        "binding": project_binding(runner.config.projects["project"]),
        "tool": tool,
        "arguments": arguments,
    }


def delivery(runner, job_id="security-job"):
    row = runner.db.execute("SELECT status,result FROM deliveries WHERE id=?", (job_id,)).fetchone()
    return row["status"], json.loads(row["result"])


def git(root, *args, content=None):
    return subprocess.run(
        [shutil.which("git"), *args],
        cwd=root,
        input=content,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


@pytest.mark.skipif(os.name == "nt", reason="Unix executable verifier fixture")
async def test_readonly_git_log_does_not_execute_signature_verifier(runner):
    root = runner.config.projects["project"]
    git(root, "init", "-q")
    tree = git(root, "mktree", content="")
    commit = git(
        root,
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        content=(
            f"tree {tree}\nauthor Fixture <fixture@example.invalid> 1 +0000\n"
            "committer Fixture <fixture@example.invalid> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n ZmFrZQ==\n -----END PGP SIGNATURE-----\n\nfixture\n"
        ),
    )
    git(root, "update-ref", "HEAD", commit)
    verifier = root / "verifier"
    verifier.write_text("#!/bin/sh\nprintf called > verifier-called\ncat >/dev/null\nexit 1\n")
    verifier.chmod(0o755)
    git(root, "config", "log.showSignature", "true")
    git(root, "config", "gpg.program", str(verifier))
    runner.config.permissions = {"read"}
    await runner.execute(job(runner, "git_log", {}))
    status, result = delivery(runner)
    assert status == "succeeded" and "fixture" in result["log"]
    assert not (root / "verifier-called").exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix lazy-fetch transport fixture")
@pytest.mark.parametrize("transport", ["ssh", "ext"])
async def test_readonly_git_log_does_not_execute_lazy_fetch_transport(runner, transport):
    root = runner.config.projects["project"]
    git(root, "init", "-q")
    script = root / "transport"
    script.write_text("#!/bin/sh\nprintf called > transport-called\nexit 1\n")
    script.chmod(0o755)
    git(root, "config", "remote.origin.promisor", "true")
    git(root, "config", "protocol." + transport + ".allow", "always")
    git(root, "config", "core.sshCommand", str(script))
    url = "ssh://fixture.invalid/repo" if transport == "ssh" else "ext::" + str(script)
    git(root, "config", "remote.origin.url", url)
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    (root / ".git/refs/heads/main").write_text("1" * 40 + "\n")
    # Even an operator environment override must not turn a read into a transport call.
    runner.config.environment["GIT_ALLOW_PROTOCOL"] = transport
    runner.config.permissions = {"read"}
    await runner.execute(job(runner, "git_log", {}))
    assert delivery(runner)[0] == "failed"
    assert not (root / "transport-called").exists()


async def test_project_skill_read_retains_task_binding(runner, monkeypatch):
    root = runner.config.projects["project"]
    relative = Path(".agents/skills/probe")
    skill = root / relative
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe\ndescription: fixture\n---\nsafe\n")
    extensions = runner.extensions["project"]
    original = extensions.skill_root

    def replace_after_discovery(*args):
        result = original(*args)
        root.rename(root.with_name("old-project"))
        replacement = root / relative
        replacement.mkdir(parents=True)
        (replacement / "SKILL.md").write_text("must-not-read")
        return result

    monkeypatch.setattr(extensions, "skill_root", replace_after_discovery)
    await runner.execute(job(runner, "skill_read", {"name": "project:probe"}))
    assert delivery(runner)[0] == "failed"
    assert "must-not-read" not in json.dumps(delivery(runner)[1])


async def test_local_extension_copy_retains_task_binding(runner, monkeypatch):
    root = runner.config.projects["project"]
    source = root / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: probe\ndescription: fixture\n---\nsafe\n")

    def replace_before_copy(selected, target):
        root.rename(root.with_name("old-project"))
        (root / "source").mkdir(parents=True)
        (root / "source/SKILL.md").write_text(
            "---\nname: probe\ndescription: fixture\n---\nmust-not-copy\n"
        )
        return copy_bundle(selected, target)

    monkeypatch.setattr("webcodex.extensions.copy_bundle", replace_before_copy)
    await runner.execute(job(runner, "extension_install", {"kind": "skill", "source": "source"}))
    assert delivery(runner)[0] == "failed"
    assert not (runner.extensions["project"].root / "skill/probe").exists()


@pytest.mark.parametrize("phase", ["init", "register"])
async def test_project_creation_rejects_replaced_child(runner, monkeypatch, tmp_path, phase):
    root = tmp_path / "projects"
    root.mkdir()
    runner.config.project_roots = {"work": root}
    restore_projects(runner.config, runner.db)
    operations = ProjectOperations(
        runner.config, runner.db, runner.processes, "security-task", project_binding(root)
    )
    target = root / "created"

    def replace():
        target.rename(root / "original")
        target.mkdir()

    if phase == "init":
        original = Workspace.directory
        captured = False
        original_binding = project_binding

        def capture_binding(path, **kwargs):
            nonlocal captured
            result = original_binding(path, **kwargs)
            if path == target:
                captured = True
            return result

        def replace_before_open(workspace, path=".", **kwargs):
            nonlocal captured
            if captured and workspace.root == root and path == "created":
                replace()
                captured = False
            return original(workspace, path, **kwargs)

        monkeypatch.setattr("webcodex.projects.project_binding", capture_binding)
        monkeypatch.setattr("webcodex.filesystem.project_binding", capture_binding)
        monkeypatch.setattr(Workspace, "directory", replace_before_open)
    else:
        original = operations.register

        def replace_before_register(*args, **kwargs):
            replace()
            return original(*args, **kwargs)

        monkeypatch.setattr(operations, "register", replace_before_register)
    with pytest.raises(OutcomeUnknown):
        await operations.project_create(
            "local/work", "created", "fixture", git_init=phase == "init"
        )
    assert not (target / ".git").exists()
    assert "created" not in runner.config.projects
    assert runner.db.execute("SELECT count(*) FROM managed_projects").fetchone()[0] == 0


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="Unix unprivileged mode checks")
def test_listing_skips_unreadable_directory_but_bundle_does_not(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "public.txt").write_text("visible")
    restricted = source / "restricted"
    restricted.mkdir()
    (restricted / "private.txt").write_text("restricted")
    restricted.chmod(0)
    try:
        workspace = Workspace(source)
        assert workspace.files(".", "*", 0, 100)["files"] == [{"path": "public.txt", "bytes": 7}]
        with pytest.raises(PermissionError):
            copy_bundle(source, tmp_path / "installed")
    finally:
        restricted.chmod(0o700)


def test_helpers_reject_project_and_relative_path_entries(runner, monkeypatch, tmp_path):
    root = runner.config.projects["project"]
    trusted = Path(shutil.which("git")).resolve()
    for name in ["git", "git.exe"]:
        candidate = root / name
        candidate.write_bytes(b"untrusted")
        candidate.chmod(0o755)
    runner.config.environment["PATH"] = os.pathsep.join([".", str(root), str(trusted.parent)])
    monkeypatch.chdir(root)
    assert Path(executable("git", runner.config)) == trusted
    runner.config.environment["PATH"] = os.pathsep.join([".", str(root)])
    with pytest.raises(ValueError, match="trusted absolute PATH"):
        executable("git", runner.config)


@pytest.mark.skipif(os.name == "nt", reason="Unix executable symlink fixture")
def test_helper_symlink_into_a_project_is_rejected(runner, tmp_path):
    root = runner.config.projects["project"]
    target = root / "untrusted-git"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "git").symlink_to(target)
    runner.config.environment["PATH"] = str(tools)
    with pytest.raises(ValueError, match="trusted absolute PATH"):
        executable("git", runner.config)


@pytest.mark.parametrize("replacement", ["directory", "link"])
async def test_runner_rejects_root_replacement_after_inventory(
    runner, monkeypatch, tmp_path, replacement
):
    root = runner.config.projects["project"]
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "sentinel.txt").write_text("must-not-return")
    request = job(runner, "read_file", {"path": "sentinel.txt"})
    inventory = runner.inventory

    def replace_after_check():
        result = inventory()
        root.rename(tmp_path / "original")
        if replacement == "link":
            directory_link(root, outside)
        else:
            root.mkdir()
            (root / "sentinel.txt").write_text("must-not-return")
        return result

    monkeypatch.setattr(runner, "inventory", replace_after_check)
    await runner.execute(request)
    status, result = delivery(runner)
    assert status == "failed" and "must-not-return" not in json.dumps(result)


def test_workspace_rejects_replaced_ancestor_and_root(tmp_path):
    parent = tmp_path / "parent"
    root = parent / "project"
    root.mkdir(parents=True)
    (root / "file.txt").write_text("original")
    workspace = Workspace(root)
    parent.rename(tmp_path / "original-parent")
    directory_link(parent, tmp_path / "original-parent")
    with pytest.raises((ValueError, OSError)):
        workspace.read("file.txt")


async def test_search_rejects_ancestor_replacement_after_enumeration(runner, monkeypatch, tmp_path):
    root = runner.config.projects["project"]
    (root / "public").mkdir()
    (root / "public/file.txt").write_text("safe")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "file.txt").write_text("must-not-return")
    original = Workspace.files

    def replace_after_enumeration(workspace, *args, **kwargs):
        page = original(workspace, *args, **kwargs)
        (root / "public").rename(root / "original-public")
        directory_link(root / "public", outside)
        return page

    monkeypatch.setattr(Workspace, "files", replace_after_enumeration)
    await runner.execute(job(runner, "search_files", {"query": "must-not-return"}))
    status, result = delivery(runner)
    assert status == "failed" and "must-not-return" not in json.dumps(result)
    assert not list(runner.processes.directory.glob("search-*"))


async def test_search_preserves_regex_large_files_and_original_names(runner):
    root = runner.config.projects["project"]
    (root / "nested").mkdir()
    (root / "nested/large.txt").write_text("filler\n" * 450000 + "marker-42\n")
    await runner.execute(job(runner, "search_files", {"query": "marker-[0-9]+", "regex": True}))
    status, result = delivery(runner)
    assert status == "succeeded"
    assert "nested/large.txt:450001:marker-42" in result["output"]
    assert not list(runner.processes.directory.glob("search-*"))


async def test_search_rejects_oversized_pages_without_leaving_snapshots(runner):
    root = runner.config.projects["project"]
    with (root / "large.txt").open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)
    await runner.execute(job(runner, "search_files", {"query": "anything"}))
    status, result = delivery(runner)
    assert status == "failed" and "20 MiB" in result["error"]
    assert not list(runner.processes.directory.glob("search-*"))


async def test_search_cleans_readonly_snapshots_and_preserves_file_pagination(runner):
    root = runner.config.projects["project"]
    readonly = root / "a.txt"
    readonly.write_text("readonly-result")
    readonly.chmod(0o444)
    (root / "dist").write_text("ordinary-file")
    workspace = Workspace(root)
    first = workspace.files(".", "*", 0, 1)
    second = workspace.files(".", "*", first["next_offset"], 1)
    assert first["files"][0]["path"] == "a.txt"
    assert second["files"][0]["path"] == "dist" and second["next_offset"] is None
    await runner.execute(job(runner, "search_files", {"query": "readonly-result"}))
    status, result = delivery(runner)
    assert status == "succeeded" and "a.txt:1:readonly-result" in result["output"]
    assert not list(runner.processes.directory.glob("search-*"))


@pytest.mark.skipif(os.name == "nt", reason="Unix delayed ripgrep fixture")
async def test_search_snapshot_survives_until_process_completion(runner, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    rg = tools / "rg"
    rg.write_text(
        f"#!{sys.executable}\nimport sys,time\nfrom pathlib import Path\n"
        "time.sleep(1.5)\nprint(Path(sys.argv[-1]).read_text())\n"
    )
    rg.chmod(0o755)
    runner.config.environment["PATH"] = str(tools)
    (runner.config.projects["project"] / "file.txt").write_text("snapshot-result")
    await runner.execute(job(runner, "search_files", {"query": "snapshot-result"}))
    status, result = delivery(runner)
    assert status == "succeeded" and result["status"] == "running"
    assert list(runner.processes.directory.glob("search-*"))
    await runner.processes.wait(result["process_id"], "security-task", 5)
    observed = runner.processes.read(result["process_id"], "security-task")
    assert "snapshot-result" in observed["output"]
    assert not list(runner.processes.directory.glob("search-*"))


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink replacement fixture")
def test_bundle_rejects_link_replacement_during_copy(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "reference.txt").write_text("safe")
    outside = tmp_path / "private.txt"
    outside.write_text("must-not-copy")
    original = Workspace.walk

    def replace_after_inspection(workspace, *args, **kwargs):
        for path, info in original(workspace, *args, **kwargs):
            if path == "reference.txt":
                (source / path).unlink()
                (source / path).symlink_to(outside)
            yield path, info

    monkeypatch.setattr(Workspace, "walk", replace_after_inspection)
    with pytest.raises((ValueError, OSError)):
        copy_bundle(source, tmp_path / "installed")
    assert not (tmp_path / "installed/reference.txt").exists()


def test_bundle_preserves_large_files_executable_mode_and_vendor_directories(tmp_path):
    source = tmp_path / "source"
    for name in ["node_modules", "dist", ".git"]:
        (source / name).mkdir(parents=True)
        (source / name / "file.txt").write_text(name)
    script = source / "script"
    script.write_bytes(b"x" * (3 * 1024 * 1024))
    script.chmod(0o755)
    target = tmp_path / "installed"
    copy_bundle(source, target)
    assert (target / "script").read_bytes() == script.read_bytes()
    assert stat.S_IMODE((target / "script").stat().st_mode) == stat.S_IMODE(script.stat().st_mode)
    assert (target / "node_modules/file.txt").read_text() == "node_modules"
    assert (target / "dist/file.txt").read_text() == "dist"
    assert not (target / ".git").exists()


def test_bundle_ignores_worktree_git_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").write_text("gitdir: ../worktree-metadata\n")
    (source / "SKILL.md").write_text("worktree-content")
    target = tmp_path / "installed"
    copy_bundle(source, target)
    assert not (target / ".git").exists()
    assert (target / "SKILL.md").read_text() == "worktree-content"


async def test_oversized_provider_result_is_unknown_and_not_replayed(runner, monkeypatch):
    calls = []

    async def request(*args, **kwargs):
        calls.append("effect")
        return {"content": [{"type": "text", "text": "x" * (2 * 1024 * 1024 + 1)}]}

    async def provider(*args, **kwargs):
        return SimpleNamespace(request=request)

    monkeypatch.setattr(runner.extensions["project"], "provider", provider)
    request_job = job(
        runner,
        "plugin_call",
        {
            "plugin": "probe",
            "server": "probe",
            "tool_name": "submit",
            "arguments": {},
        },
    )
    await runner.execute(request_job)
    status, result = delivery(runner)
    assert status == "unknown" and result["error_type"] == "OutcomeUnknown"
    assert "inspect its effects" in result["error"]
    await runner.execute(request_job)
    assert calls == ["effect"]


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows executable lookup")
async def test_windows_read_helpers_ignore_repository_executables(runner):
    root = runner.config.projects["project"]
    git(root, "init", "-q")
    shutil.copyfile(sys.executable, root / "git.exe")
    shutil.copyfile(sys.executable, root / "rg.exe")
    (root / "file.txt").write_text("real-ripgrep-result")
    runner.config.permissions = {"read"}
    await runner.execute(job(runner, "git_status", {}, "git-job"))
    assert delivery(runner, "git-job")[0] == "succeeded"
    await runner.execute(
        job(runner, "search_files", {"query": "real-ripgrep-result", "glob": "*.txt"}, "search-job")
    )
    status, result = delivery(runner, "search-job")
    assert status == "succeeded" and "file.txt:1:real-ripgrep-result" in result["output"]


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows executable lookup")
def test_windows_structured_helpers_reject_batch_wrappers(runner, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ["git", "rg"]:
        for suffix in [".cmd", ".bat"]:
            (tools / (name + suffix)).write_text("@echo off\n")
    runner.config.environment.update({"PATH": str(tools), "PATHEXT": ".CMD;.BAT;.EXE;.COM"})
    for name in ["git", "rg"]:
        with pytest.raises(ValueError, match="trusted absolute PATH"):
            executable(name, runner.config)
