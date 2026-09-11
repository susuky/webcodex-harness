from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_PERMISSIONS = {"read", "write", "execute", "git_write", "extensions"}
COMPUTER_PERMISSIONS = {"computer_observe", "computer_control"}
PERMISSIONS = DEFAULT_PERMISSIONS | COMPUTER_PERMISSIONS
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            "Identifier must contain 1–64 letters, digits, dots, underscores or hyphens"
        )
    return value


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
    permissions: set[str] = Field(default_factory=lambda: set(DEFAULT_PERMISSIONS))
    approval_required: set[str] = Field(
        default_factory=lambda: {"execute", "git_write", "extensions", "computer_control"}
    )

    @model_validator(mode="after")
    def valid_permissions(self):
        if not self.permissions <= PERMISSIONS or not self.approval_required <= PERMISSIONS:
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
    permissions: set[str] = Field(default_factory=lambda: set(DEFAULT_PERMISSIONS))
    max_concurrent_jobs: int = Field(default=4, ge=1, le=32)
    max_processes: int = Field(default=16, ge=1, le=128)
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
        for name, path in self.projects.items():
            identifier(name)
            path = path.expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Project root must be a directory")
            if self.state_dir == path or self.state_dir.is_relative_to(path):
                raise ValueError("Runner state must be outside all registered projects")
            self.projects[name] = path
        return self

    def inventory(self) -> dict:
        result = {}
        for name, path in self.projects.items():
            if path.resolve(strict=True) != path or not path.is_dir():
                raise ValueError("Registered project root was replaced by a symlink or removed")
            info = path.stat()
            identity = f"{path}\0{info.st_dev}\0{info.st_ino}"
            result[name] = hashlib.sha256(identity.encode()).hexdigest()
        return result


def load_config(path: str | Path, model: type[StrictModel]):
    try:
        return model.model_validate_json(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except ValidationError as error:
        raise ValueError(
            "Invalid configuration: "
            + json.dumps(error.errors(include_input=False, include_context=False))
        ) from None


def initialize(
    directory: Path, project: Path, port: int, trusted: bool, computer_use: bool = False
) -> dict:
    directory = directory.expanduser().resolve()
    project = project.expanduser().resolve(strict=True)
    # Credentials and execution journals cannot be exposed through the project tools.
    if directory == project or directory.is_relative_to(project):
        raise ValueError("Choose a config directory outside the registered project")
    private_dir(directory)
    for name in ["server.json", "runner.json", "client.json"]:
        if (directory / name).exists():
            raise ValueError(f"Refusing to replace existing {name}")
    admin, client, runner = [secrets.token_urlsafe(32) for _ in range(3)]
    url = f"http://127.0.0.1:{port}"
    policy = Client(token=client, projects=["local/project"])
    if computer_use:
        policy.permissions |= COMPUTER_PERMISSIONS
    if trusted:
        policy.approval_required = set()
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
        projects={"project": project},
        environment={"WEBCODEX_PYTHON": sys.executable},
    )
    if computer_use:
        runner_config.permissions |= COMPUTER_PERMISSIONS
    write_private(directory / "server.json", server.model_dump(mode="json"))
    write_private(directory / "runner.json", runner_config.model_dump(mode="json"))
    write_private(directory / "client.json", {"server_url": url, "token": client})
    return {"config_dir": str(directory), "mcp_url": url + "/mcp", "project": "local/project"}
