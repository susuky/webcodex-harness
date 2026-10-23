from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_PERMISSIONS = {"read", "write", "execute", "git_write", "extensions"}
COMPUTER_PERMISSIONS = {"computer_observe", "computer_control"}
PERMISSIONS = DEFAULT_PERMISSIONS | COMPUTER_PERMISSIONS | {"projects"}
MAX_PROJECTS = 100
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            "Identifier must contain 1–64 letters, digits, dots, underscores or hyphens"
        )
    return value


def project_binding(path: Path, *, info: os.stat_result | None = None) -> str:
    if info is None:
        if path.resolve(strict=True) != path or not path.is_dir():
            raise ValueError("Project directory was replaced by a symlink or removed")
        info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Project directory was replaced or removed")
    return hashlib.sha256(f"{path}\0{info.st_dev}\0{info.st_ino}".encode()).hexdigest()


def server_url(value: str) -> str:
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError(
            "Server URL must be an origin without credentials, query, fragment or path"
        )
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Remote Server connections require HTTPS")
    return value.rstrip("/")


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if is_link(path):
        raise ValueError("State directory must not be a symlink")
    if os.name == "nt":
        from .windows import private_acl

        private_acl(path)
    else:
        path.chmod(0o700)
    return path


def is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Client(StrictModel):
    token: str = Field(min_length=32)
    projects: list[str]
    project_roots: list[str] = Field(default_factory=list)
    permissions: set[str] = Field(default_factory=lambda: set(DEFAULT_PERMISSIONS))

    @model_validator(mode="before")
    @classmethod
    def legacy_approval_settings(cls, value):
        # Load existing installations without reviving the removed approval gate.
        if isinstance(value, dict):
            return {
                key: item
                for key, item in value.items()
                if key not in {"approval_required", "trusted_projects", "trusted_project_roots"}
            }
        return value

    @model_validator(mode="after")
    def valid_permissions(self):
        if not self.permissions <= PERMISSIONS:
            raise ValueError("Unknown permission")
        return self


class ServerConfig(StrictModel):
    state_dir: Path
    admin_token: str = Field(min_length=32)
    clients: dict[str, Client]
    runners: dict[str, str]
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    public_url: str = "http://127.0.0.1:8765"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "[::1]"])
    max_pending_jobs: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def credentials(self):
        tokens = [
            self.admin_token,
            *self.runners.values(),
            *(c.token for c in self.clients.values()),
        ]
        if len(tokens) != len(set(tokens)) or any(len(t) < 32 for t in tokens):
            raise ValueError(
                "Admin, client and each Runner require distinct tokens of at least 32 characters"
            )
        for name in [*self.clients, *self.runners]:
            identifier(name)
        self.state_dir = self.state_dir.expanduser().resolve()
        self.public_url = server_url(self.public_url)
        return self


class RunnerConfig(StrictModel):
    server_url: str
    runner_id: str
    token: str = Field(min_length=32)
    state_dir: Path
    projects: dict[str, Path]
    project_roots: dict[str, Path] = Field(default_factory=dict)
    permissions: set[str] = Field(default_factory=lambda: set(DEFAULT_PERMISSIONS))
    max_concurrent_jobs: int = Field(default=4, ge=1, le=32)
    max_processes: int = Field(default=16, ge=1, le=128)
    subagents_enabled: bool = False
    max_subagents: int = Field(default=4, ge=1, le=16)
    max_output_bytes: int = Field(default=8 * 1024 * 1024, ge=4096, le=128 * 1024 * 1024)
    max_process_seconds: int = Field(default=3600, ge=1, le=86400)
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def boundaries(self):
        identifier(self.runner_id)
        if not self.permissions <= PERMISSIONS:
            raise ValueError("Unknown permission")
        self.server_url = server_url(self.server_url)
        self.state_dir = self.state_dir.expanduser().resolve()
        for paths in (self.projects, self.project_roots):
            if len(paths) > MAX_PROJECTS:
                raise ValueError(
                    f"At most {MAX_PROJECTS} projects or project roots may be configured"
                )
            for name, path in paths.items():
                identifier(name)
                path = path.expanduser().resolve()
                if path.exists() and not path.is_dir():
                    raise ValueError("Project root must be a directory")
                if self.state_dir == path or self.state_dir.is_relative_to(path):
                    raise ValueError(
                        "Runner state must be outside all registered projects and roots"
                    )
                paths[name] = path
        return self

    def inventory(self) -> dict:
        return {name: project_binding(path) for name, path in self.projects.items()}


def load_config(path: str | Path, model: type[StrictModel]):
    try:
        return model.model_validate_json(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except ValidationError as error:
        raise ValueError(
            "Invalid configuration: "
            + json.dumps(error.errors(include_input=False, include_context=False))
        ) from None


def allow_project_root(
    directory: Path,
    path: Path,
    *,
    client_id: str = "owner",
    name: str | None = None,
    trusted: bool = False,
) -> dict:
    """Operator-only setup; preserve credentials and unrelated clients, with private backups."""
    directory = directory.expanduser().resolve(strict=True)
    path = path.expanduser().resolve(strict=True)
    if not path.is_dir() or directory.is_relative_to(path):
        raise ValueError("Choose a project directory that does not contain WebCodex settings")
    paths = {key: directory / (key + ".json") for key in ("server", "runner")}
    if any(is_link(p) for p in paths.values()):
        raise ValueError("Configuration files must not be symlinks")
    originals = {key: p.read_bytes() for key, p in paths.items()}
    data = {key: json.loads(content.decode("utf-8-sig")) for key, content in originals.items()}
    server = ServerConfig.model_validate(data["server"])
    runner = RunnerConfig.model_validate(data["runner"])
    if client_id not in server.clients:
        raise ValueError(
            "Choose an existing client with --client; available: " + ", ".join(server.clients)
        )
    if server.runners.get(runner.runner_id) != runner.token:
        raise ValueError("Server and Runner settings do not belong to the same installation")
    existing = next((key for key, value in runner.project_roots.items() if value == path), None)
    name = existing or name or re.sub(r"[^A-Za-z0-9_.-]", "-", path.name).strip(".-_") or "projects"
    identifier(name)
    if name in runner.project_roots and runner.project_roots[name] != path:
        raise ValueError("This root name is already used; choose a different --name")
    root_id = runner.runner_id + "/" + name
    policy = data["server"]["clients"][client_id]
    policy["project_roots"] = sorted(set(policy.get("project_roots", [])) | {root_id})
    permissions = {"projects"}
    policy["permissions"] = sorted(server.clients[client_id].permissions | permissions)
    data["runner"].setdefault("project_roots", {})[name] = str(path)
    data["runner"]["permissions"] = sorted(runner.permissions | permissions)
    data["server"] = ServerConfig.model_validate(data["server"]).model_dump(mode="json")
    RunnerConfig.model_validate(data["runner"])
    backup = private_dir(directory / "backups" / ("access-" + secrets.token_hex(8)))
    encoded = {
        key: (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        for key, value in data.items()
    }
    for key, content in originals.items():
        with (backup / (key + ".json")).open("xb") as stream:
            stream.write(content)
        (backup / (key + ".json")).chmod(0o600)

    def replace(key: str, content: bytes, expected: bytes):
        fd, temporary = tempfile.mkstemp(prefix=".access-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if is_link(paths[key]) or paths[key].read_bytes() != expected:
                raise ValueError("Settings changed during setup; retry using the current files")
            os.replace(temporary, paths[key])
        finally:
            Path(temporary).unlink(missing_ok=True)

    written = []
    try:
        for key in ("runner", "server"):
            replace(key, encoded[key], originals[key])
            written.append(key)
    except Exception:
        for key in reversed(written):
            replace(key, originals[key], encoded[key])
        raise
    return {
        "project_root": root_id,
        "path": str(path),
        "client": client_id,
        "backup": str(backup),
        "restart_required": ["server", "runner"],
    }


def initialize(
    directory: Path,
    project: Path | None,
    port: int,
    trusted: bool = False,
    computer_use: bool = False,
    project_root: Path | None = None,
) -> dict:
    directory = directory.expanduser().resolve()
    if project is None and project_root is None:
        raise ValueError("Choose an existing --project or an allowed --project-root")
    project = project.expanduser().resolve(strict=True) if project else None
    project_root = project_root.expanduser().resolve(strict=True) if project_root else None
    # Credentials and execution journals cannot be exposed through the project tools.
    for root in (project, project_root):
        if root and (directory == root or directory.is_relative_to(root)):
            raise ValueError("Choose a config directory outside the registered project and roots")
    private_dir(directory)
    for name in ["server.json", "runner.json", "client.json"]:
        if (directory / name).exists():
            raise ValueError(f"Refusing to replace existing {name}")
    admin, client, runner = [secrets.token_urlsafe(32) for _ in range(3)]
    url = f"http://127.0.0.1:{port}"
    policy = Client(token=client, projects=["local/project"] if project else [])
    if project_root:
        policy.project_roots = ["local/projects"]
        policy.permissions.add("projects")
    if computer_use:
        policy.permissions |= COMPUTER_PERMISSIONS
    server = ServerConfig(
        state_dir=directory / "server",
        admin_token=admin,
        clients={"owner": policy},
        runners={"local": runner},
        port=port,
        public_url=url,
    )
    runner_config = RunnerConfig(
        state_dir=directory / "runner",
        server_url=url,
        runner_id="local",
        token=runner,
        projects={"project": project} if project else {},
        project_roots={"projects": project_root} if project_root else {},
        environment={"WEBCODEX_PYTHON": sys.executable},
    )
    if computer_use:
        runner_config.permissions |= COMPUTER_PERMISSIONS
    if project_root:
        runner_config.permissions.add("projects")
    write_private(directory / "server.json", server.model_dump(mode="json"))
    write_private(directory / "runner.json", runner_config.model_dump(mode="json"))
    write_private(directory / "client.json", {"server_url": url, "token": client})
    return {
        "config_dir": str(directory),
        "mcp_url": url + "/mcp",
        "project": "local/project" if project else None,
        "project_root": "local/projects" if project_root else None,
    }
