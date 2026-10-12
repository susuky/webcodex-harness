from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import Field

from .catalog import OutcomeUnknown, definitions, tool
from .config import MAX_PROJECTS, RunnerConfig, identifier, project_binding
from .filesystem import Workspace
from .processes import Processes, executable
from .store import new_id

logger = logging.getLogger(__name__)
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
RequestID = Annotated[str, Field(min_length=1, max_length=128)]


def restore_projects(config: RunnerConfig, db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS managed_projects (
        name TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL,
        binding TEXT NOT NULL, root_binding TEXT NOT NULL
    )""")
    db.commit()
    restored = {}
    for row in db.execute("SELECT * FROM managed_projects ORDER BY name"):
        if row["name"] in config.projects:
            raise ValueError("Managed project ID conflicts with a configured project")
        try:
            root = config.project_roots[row["root"]]
            path = Workspace(root).path(row["path"], directory=True)
            if (
                project_binding(root) != row["root_binding"]
                or project_binding(path) != row["binding"]
            ):
                raise ValueError("Project or parent directory identity changed")
            restored[row["name"]] = path
        except (KeyError, ValueError, OSError):
            logger.warning("Managed project %s is unavailable; registration retained", row["name"])
    total = len(config.projects) + len(restored)
    if total > MAX_PROJECTS:
        raise ValueError(
            f"Configured and available managed projects total {total}; this Runner supports "
            f"at most {MAX_PROJECTS}. Reduce configured projects in runner.json before restarting; "
            "managed registrations are retained."
        )
    config.projects.update(restored)


def root_inventory(config: RunnerConfig, db: sqlite3.Connection) -> dict:
    result = {}
    for name, path in config.project_roots.items():
        try:
            result[name] = {"binding": project_binding(path), "projects": []}
        except (ValueError, OSError):
            continue
    managed = set()
    for row in db.execute("SELECT * FROM managed_projects ORDER BY name"):
        managed.add(row["name"])
        root = result.get(row["root"])
        path = config.projects.get(row["name"])
        try:
            if (
                root
                and path
                and root["binding"] == row["root_binding"]
                and project_binding(path) == row["binding"]
            ):
                root["projects"].append(row["name"])
        except (ValueError, OSError):
            continue
    # Configured projects beneath a granted root follow the same policy as newly registered ones.
    for name, path in config.projects.items():
        if name in managed:
            continue
        try:
            project_binding(path)
        except (ValueError, OSError):
            continue
        parents = [
            key
            for key in result
            if path != config.project_roots[key] and path.is_relative_to(config.project_roots[key])
        ]
        if parents:
            key = max(parents, key=lambda key: len(config.project_roots[key].parts))
            result[key]["projects"].append(name)
    return result


class ProjectOperations:
    def __init__(
        self,
        config: RunnerConfig,
        db: sqlite3.Connection,
        processes: Processes,
        task_id: str,
        expected_binding: str | None = None,
    ):
        self.config, self.db, self.processes, self.task_id = config, db, processes, task_id
        self.expected_binding = expected_binding

    def parent(self, root: str, name: str) -> tuple[str, Workspace]:
        runner, key = root.split("/", 1)
        identifier(key)
        identifier(name)
        if runner != self.config.runner_id or key not in self.config.project_roots:
            raise ValueError("Project root is not allowed by this Runner")
        if (
            name in self.config.projects
            or self.db.execute("SELECT 1 FROM managed_projects WHERE name=?", (name,)).fetchone()
        ):
            raise ValueError("Project ID is already registered; use projects_list")
        if (
            len(self.config.projects) >= MAX_PROJECTS
            or self.db.execute("SELECT count(*) FROM managed_projects").fetchone()[0]
            >= MAX_PROJECTS
        ):
            raise ValueError(f"This Runner has reached its {MAX_PROJECTS}-project limit")
        workspace = Workspace(self.config.project_roots[key], self.expected_binding)
        workspace.parts(name)
        return key, workspace

    def register(
        self,
        key: str,
        name: str,
        workspace: Workspace,
        path: str,
        binding: str,
        expected_binding: str | None = None,
    ) -> dict:
        target = workspace.path(path, directory=True)
        if target in self.config.projects.values():
            raise ValueError("Directory is already registered under another project ID")
        if project_binding(workspace.root) != binding:
            raise OutcomeUnknown("Project root changed; inspect the directory before retrying")
        with workspace.directory(path) as directory:
            actual = project_binding(target, info=workspace.directory_info(directory))
            if expected_binding is not None and actual != expected_binding:
                raise ValueError("Created directory identity changed")
        with self.db:
            self.db.execute(
                "INSERT INTO managed_projects VALUES (?,?,?,?,?)",
                (name, key, path, actual, binding),
            )
        self.config.projects[name] = target
        return {
            "project": self.config.runner_id + "/" + name,
            "root": self.config.runner_id + "/" + key,
            "path": path,
            "registered": True,
        }

    @tool("projects")
    async def project_create(
        self, root: str, name: Name, request_id: RequestID, git_init: bool = False
    ) -> dict:
        """Create a new directory and persistently register it on the Runner under an exact root ID from project_roots_list. No existing task or project is needed. name is both the new project ID and a direct child directory; existing paths are never adopted or overwritten. git_init is an explicit optional action and creates no commit. Reuse request_id for the same request within this root; poll the returned job_id, then use the resulting project ID with task_create. Creation requires projects permission and operator approval by default."""
        key, workspace = self.parent(root, name)
        binding = workspace.binding
        with workspace.parent(name) as (parent, child):
            if os.name == "nt":
                (parent / child).mkdir()
            else:
                os.mkdir(child, dir_fd=parent)
        try:
            created = workspace.child(name)
            target, created_binding = created.root, created.binding
            if git_init:
                with created.directory() as directory:
                    cwd = target if os.name == "nt" else Path(f"/proc/{os.getpid()}/fd/{directory}")
                    process = await self.processes.start(
                        new_id(),
                        self.task_id,
                        [
                            executable("git", self.config),
                            "-c",
                            "core.bare=false",
                            "-c",
                            f"core.hooksPath={os.devnull}",
                            "-c",
                            "core.fsmonitor=false",
                            "init",
                            "--template=",
                            "--initial-branch=main",
                            "--",
                            ".",
                        ],
                        cwd,
                        60,
                    )
                    state = self.processes.active.get(process)
                    if state:
                        state["proc"].stdin.close()
                    await self.processes.wait(process, self.task_id, 65)
                result = self.processes.read(process, self.task_id)
                if result["status"] != "completed" or result["exit_code"] != 0:
                    raise ValueError(f"Git initialization did not complete; process_id={process}")
            return {
                **self.register(key, name, workspace, name, binding, created_binding),
                "created_directory": True,
                "git_initialized": git_init,
            }
        except Exception as error:
            raise OutcomeUnknown(
                "Directory creation started; inspect projects_list and the target before retrying. "
                "No files were removed. " + str(error)
            ) from error

    @tool("projects")
    async def project_register(
        self,
        root: str,
        name: Name,
        path: Annotated[str, Field(min_length=1, max_length=4096)],
        request_id: RequestID,
    ) -> dict:
        """Persistently register an existing directory beneath an exact root ID from project_roots_list. path is relative to that root; traversal, symlinks and registering the root itself are rejected. Does not modify project files or initialize Git. No existing task is needed. Reuse request_id within this root and poll the returned job_id before using the resulting project ID with task_create. Requires projects permission and operator approval by default."""
        key, workspace = self.parent(root, name)
        workspace.parts(path)
        return {
            **self.register(key, name, workspace, path, workspace.binding),
            "created_directory": False,
        }


PROJECT_OPERATIONS = definitions(ProjectOperations, remote=False)
