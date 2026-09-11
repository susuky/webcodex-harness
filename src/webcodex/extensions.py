from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .catalog import OutcomeUnknown
from .config import RunnerConfig, identifier, is_link, private_dir
from .filesystem import Workspace
from .processes import environment


def skill_metadata(workspace: Workspace, path: str = "SKILL.md") -> dict:
    text = workspace.read(path, 128 * 1024).decode("utf-8")
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        raise ValueError("SKILL.md requires YAML frontmatter")
    data = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(data, dict) or not isinstance(data.get("description"), str):
        raise ValueError("Skill frontmatter requires name and description strings")
    identifier(data.get("name", ""))
    if not data["description"].strip() or len(data["description"]) > 4096:
        raise ValueError("Skill description must contain 1–4096 characters")
    return {"name": data["name"], "description": data["description"], "instructions": text}


def plugin_manifest(root: Path) -> dict:
    workspace = Workspace(root)
    candidate = next(
        (
            p
            for p in [".codex-plugin/plugin.json", ".claude-plugin/plugin.json"]
            if (root / p).exists()
        ),
        None,
    )
    if not candidate:
        raise ValueError("Plugin requires .codex-plugin/plugin.json or .claude-plugin/plugin.json")
    manifest = json.loads(workspace.read(candidate, 64 * 1024))
    identifier(manifest.get("name", ""))
    servers = manifest.get("mcpServers", ".mcp.json")
    if isinstance(servers, str):
        workspace.path(servers)
        if (root / servers).exists():
            servers = json.loads(workspace.read(servers, 128 * 1024))
        elif servers == ".mcp.json":
            servers = {}
        else:
            raise ValueError("The plugin's MCP configuration file does not exist")
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object or relative JSON filename")
    servers = servers.get("mcpServers", servers)
    if not isinstance(servers, dict) or len(servers) > 16:
        raise ValueError("A plugin may declare at most 16 MCP servers")
    for name, spec in servers.items():
        identifier(name)
        if not isinstance(spec, dict) or bool(spec.get("command")) == bool(spec.get("url")):
            raise ValueError("Each MCP provider requires either command or url")
        if spec.get("url") and urlsplit(spec["url"]).scheme not in {"http", "https"}:
            raise ValueError("MCP provider URLs must use HTTP or HTTPS")
        if not isinstance(spec.get("args", []), list) or not all(
            isinstance(s, str) for s in spec.get("args", [])
        ):
            raise ValueError("MCP provider args must be a list of strings")
    skills = manifest.get("skills", "skills")
    skills = [skills] if isinstance(skills, str) else skills
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        raise ValueError("Plugin skills must be relative directories")
    for path in skills:
        workspace.path(path)
    return {
        "name": manifest["name"],
        "description": manifest.get("description", ""),
        "version": manifest.get("version"),
        "servers": servers,
        "skills": skills,
    }


def copy_bundle(source: Path, target: Path):
    count, total = 0, 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*dirs, *files]:
            info = (Path(directory) / name).lstat()
            if (
                is_link(Path(directory) / name)
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or info.st_nlink > 1
                and stat.S_ISREG(info.st_mode)
            ):
                raise ValueError(
                    "Extension bundles cannot contain symlinks, hardlinks or special files"
                )
            count += 1
            total += info.st_size if stat.S_ISREG(info.st_mode) else 0
            if count > 2000 or total > 20 * 1024 * 1024:
                raise ValueError("Extension exceeds the 2,000-file / 20 MiB installation limit")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))


class Provider:
    """One owner task keeps the SDK's async context and provider session alive."""

    def __init__(self, spec: dict, root: Path, project: Path, config: RunnerConfig):
        self.spec, self.root, self.project, self.config = spec, root, project, config
        self.queue = asyncio.Queue(maxsize=16)
        self.task = asyncio.create_task(self._serve())

    def expand(self, value: str) -> str:
        variables = {
            "PLUGIN_ROOT": str(self.root),
            "CODEX_PLUGIN_ROOT": str(self.root),
            "CLAUDE_PLUGIN_ROOT": str(self.root),
            "PROJECT_ROOT": str(self.project),
            **self.config.environment,
        }

        def replace(match):
            key = match.group(1)
            if key not in variables:
                raise ValueError(
                    f"Configure Runner environment variable {key} before using this plugin"
                )
            return variables[key]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)

    async def _serve(self):
        pending = None
        try:
            async with AsyncExitStack() as stack:
                spec = self.spec
                if spec.get("command"):
                    env = environment(self.config)
                    env.update({k: self.expand(v) for k, v in spec.get("env", {}).items()})
                    # Provider stderr can contain credentials; it does not enter Server logs.
                    errlog = stack.enter_context(await asyncio.to_thread(open, os.devnull, "w"))
                    streams = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=self.expand(spec["command"]),
                                args=[self.expand(v) for v in spec.get("args", [])],
                                cwd=self.project,
                                env=env,
                            ),
                            errlog=errlog,
                        )
                    )
                else:
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers={k: self.expand(v) for k, v in spec.get("headers", {}).items()},
                            timeout=60,
                            trust_env=False,
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(
                            self.expand(spec["url"]),
                            http_client=client,
                        )
                    )
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await asyncio.wait_for(session.initialize(), 20)
                while True:
                    method, arguments, pending = await self.queue.get()
                    if pending.cancelled():
                        pending = None
                        continue
                    try:
                        result = await asyncio.wait_for(getattr(session, method)(**arguments), 120)
                        if not pending.done():
                            pending.set_result(result.model_dump(mode="json", by_alias=True))
                    except Exception as error:
                        if not pending.done():
                            pending.set_exception(
                                OutcomeUnknown(
                                    f"MCP provider call failed ({type(error).__name__}); its outcome may be uncertain"
                                )
                            )
                    pending = None
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if pending and not pending.done():
                pending.set_exception(
                    OutcomeUnknown("Provider disconnected; outcome may be uncertain")
                )
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(
                        ValueError(
                            "MCP provider failed to connect; check its command, credentials and dependencies"
                        )
                    )

    async def request(self, method: str, **arguments) -> dict:
        if self.task.done():
            raise ValueError(
                "Provider is disconnected; retry discovery to start a new provider session"
            )
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((method, arguments, future))
        except asyncio.QueueFull as error:
            raise ValueError("Provider call queue is full") from error
        try:
            return await asyncio.wait_for(future, 145)
        except TimeoutError as error:
            raise OutcomeUnknown(
                "Provider timed out; do not replay a modifying tool without checking its result"
            ) from error

    async def close(self):
        self.task.cancel()
        await self.task


class Extensions:
    def __init__(self, config: RunnerConfig, project: str):
        self.config = config
        self.root = private_dir(config.state_dir / "extensions" / identifier(project))
        for kind in ["skill", "plugin"]:
            private_dir(self.root / kind)
        self.providers: dict[tuple[str, str, str], Provider] = {}

    async def install(
        self, workspace: Workspace, kind: str, source: str, ref: str | None, subpath: str, run
    ) -> dict:
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            temp = Path(temporary)
            if source.startswith("https://"):
                url = urlsplit(source)
                if url.username or url.password or url.query or url.fragment:
                    raise ValueError("Git source must be a credential-free HTTPS URL")
                if not ref or not re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                    raise ValueError(
                        "Git installation requires an exact 40-character commit SHA in ref"
                    )
                checkout = temp / "checkout"
                await run(
                    [
                        "git",
                        "-c",
                        "protocol.file.allow=never",
                        "clone",
                        "--no-checkout",
                        "--filter=blob:none",
                        "--depth=1",
                        "--",
                        source,
                        str(checkout),
                    ],
                    workspace.root,
                )
                await run(
                    ["git", "-c", "protocol.file.allow=never", "fetch", "--depth=1", "origin", ref],
                    checkout,
                )
                actual = await run(["git", "rev-parse", "FETCH_HEAD"], checkout)
                if actual.strip().lower() != ref.lower():
                    raise ValueError("Fetched commit does not match ref")
                await run(
                    ["git", "-c", f"core.hooksPath={os.devnull}", "checkout", "--detach", ref],
                    checkout,
                )
                source_root = Workspace(checkout).path(subpath, directory=True)
                resolved_ref = actual.strip()
            else:
                if ref is not None:
                    raise ValueError("ref is only valid for HTTPS Git sources")
                source_root = workspace.path(source, directory=True)
                source_root = Workspace(source_root).path(subpath, directory=True)
                resolved_ref = None
            staged = temp / "bundle"
            await asyncio.to_thread(copy_bundle, source_root, staged)
            metadata = (
                skill_metadata(Workspace(staged)) if kind == "skill" else plugin_manifest(staged)
            )
            name = metadata["name"]
            target = self.root / kind / name
            if target.exists():
                raise ValueError(
                    "Extension is already installed; explicitly remove it before replacing it"
                )
            # Keep origin alongside the bundle; never execute installation hooks.
            origin = {
                "kind": kind,
                "name": name,
                "source": source,
                "ref": resolved_ref,
                "subpath": subpath,
            }
            (staged / ".webcodex-origin.json").write_text(
                json.dumps(origin, ensure_ascii=False), encoding="utf-8"
            )
            staged.rename(target)
        return {**origin, "installed": True}

    def plugins(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "plugin").iterdir()):
            manifest = plugin_manifest(path)
            result.append(
                {k: v for k, v in manifest.items() if k not in {"servers", "skills"}}
                | {
                    "providers": list(manifest["servers"]),
                    "origin": json.loads(
                        (path / ".webcodex-origin.json").read_text(encoding="utf-8")
                    ),
                }
            )
        return result

    def skills(self, workspace: Workspace) -> tuple[list[dict], dict[str, Path]]:
        candidates = []
        for prefix in [".agents/skills", ".codex/skills", ".claude/skills"]:
            try:
                directory = workspace.path(prefix, directory=True)
            except ValueError:
                continue
            candidates.extend(
                ("project:", p)
                for p in sorted(directory.iterdir())
                if p.is_dir() and not is_link(p)
            )
        candidates.extend(("installed:", p) for p in sorted((self.root / "skill").iterdir()))
        for plugin in sorted((self.root / "plugin").iterdir()):
            manifest = plugin_manifest(plugin)
            for path in manifest["skills"]:
                directory = Workspace(plugin).path(path)
                if (directory / "SKILL.md").is_file():
                    candidates.append((manifest["name"] + ":", directory))
                elif directory.is_dir():
                    candidates.extend(
                        (manifest["name"] + ":", p)
                        for p in sorted(directory.iterdir())
                        if p.is_dir() and not is_link(p)
                    )
        result, paths = [], {}
        for prefix, path in candidates[:500]:
            if not (path / "SKILL.md").is_file():
                continue
            try:
                data = skill_metadata(Workspace(path))
                name = prefix + data["name"]
                if name in paths:
                    continue
                paths[name] = path
                result.append({"name": name, "description": data["description"]})
            except (ValueError, OSError, yaml.YAMLError) as error:
                result.append({"name": prefix + path.name, "error": str(error)[:500]})
        return result, paths

    def skill_root(self, workspace: Workspace, name: str) -> Path:
        _, paths = self.skills(workspace)
        if name not in paths:
            raise ValueError("Skill not found; call skills_list first")
        return paths[name]

    async def provider(self, task: str, workspace: Workspace, plugin: str, server: str) -> Provider:
        identifier(plugin)
        identifier(server)
        root = self.root / "plugin" / plugin
        manifest = plugin_manifest(root)
        if server not in manifest["servers"]:
            raise ValueError("MCP provider not found in installed plugin")
        key = (task, plugin, server)
        provider = self.providers.get(key)
        if provider and not provider.task.done():
            return provider
        if len([p for p in self.providers.values() if not p.task.done()]) >= 16:
            raise ValueError(
                "16 provider sessions are active; close unused sessions with plugin_disconnect"
            )
        provider = Provider(manifest["servers"][server], root, workspace.root, self.config)
        self.providers[key] = provider
        return provider

    async def disconnect(self, task: str, plugin: str, server: str):
        provider = self.providers.pop((task, plugin, server), None)
        if provider:
            await provider.close()

    async def remove(self, kind: str, name: str):
        identifier(name)
        root = self.root / kind / name
        if not root.is_dir() or is_link(root):
            raise ValueError("Installed extension not found")
        for key in list(self.providers):
            if kind == "plugin" and key[1] == name:
                await self.disconnect(*key)
        shutil.rmtree(root)

    async def close(self):
        await asyncio.gather(*(p.close() for p in self.providers.values()), return_exceptions=True)
