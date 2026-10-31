"""Local installation checks, redacted reports and bounded state maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import __version__
from .config import RunnerConfig, ServerConfig, is_link, load_config, private_dir, server_url


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a configuration object")
    return value


@contextmanager
def _readonly(path: Path):
    if is_link(path):
        raise ValueError("State databases must not be links")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        yield db
    finally:
        db.close()


def update_check(directory: Path) -> dict:
    """Read journals without constructing a Runner or changing process states."""
    directory = directory.expanduser().resolve()
    running, deliveries, errors = [], [], []
    config_path = directory / "runner.json"
    if config_path.exists():
        try:
            config = load_config(config_path, RunnerConfig)
            process_db = config.state_dir / "processes" / "processes.sqlite3"
            if process_db.exists():
                with _readonly(process_db) as db:
                    running = [
                        {"process_id": r[0], "task_id": r[1], "created": r[2]}
                        for r in db.execute(
                            "SELECT id,task_id,created FROM processes WHERE status='running'"
                        )
                    ]
            delivery_db = config.state_dir / "deliveries.sqlite3"
            if delivery_db.exists():
                with _readonly(delivery_db) as db:
                    deliveries = [
                        r[0] for r in db.execute("SELECT id FROM deliveries WHERE status='running'")
                    ]
        except (OSError, ValueError, sqlite3.Error) as error:
            errors.append(type(error).__name__)
    else:
        errors.append("missing_runner_config")
    return {
        "runner_restart_ready": not running and not deliveries and not errors,
        "running_processes": running,
        "running_deliveries": deliveries,
        "errors": errors,
        "checked_at": time.time(),
    }


def _configuration_error(error: Exception) -> dict:
    details = {"error": type(error).__name__}
    if isinstance(error, json.JSONDecodeError):
        details.update(line=error.lineno, column=error.colno)
    elif isinstance(error, ValidationError):
        # Dynamic client/project names may contain private paths or credentials.
        public_fields = set(ServerConfig.model_fields) | set(RunnerConfig.model_fields)
        fields = []
        for item in error.errors(include_url=False, include_input=False)[:20]:
            field = ".".join(
                str(part) if isinstance(part, int) or part in public_fields else "*"
                for part in item["loc"]
            )
            value = {"field": field or "<configuration>", "type": item["type"]}
            constraints = {
                key: constraint
                for key, constraint in item.get("ctx", {}).items()
                if key in {"min_length", "max_length", "ge", "gt", "le", "lt"}
                and type(constraint) in {int, float}
            }
            if constraints:
                value["constraints"] = constraints
            fields.append(value)
        details["fields"] = fields
    return details


def doctor(directory: Path, *, share: bool = False) -> dict:
    from .computer import Computer

    directory = directory.expanduser().resolve()
    report = {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "python_executable": str(Path(sys.executable)),
        "supported_runner": sys.platform in {"linux", "win32"},
        "wsl": sys.platform == "linux" and "microsoft" in platform.release().lower(),
        "command_shell": "powershell" if os.name == "nt" else "bash",
        "terminal": "conpty" if os.name == "nt" else "pty",
        "config_dir": str(directory),
        "computer": Computer().status(),
        "tools": {
            name: shutil.which(name)
            for name in (
                "git",
                "rg",
                "python3",
                "python",
                "pytest",
                "cargo",
                "node",
                "npm",
                "docker",
            )
        },
        "checks": [],
    }
    checks = report["checks"]
    for name in ("git", "rg"):
        checks.append(
            {
                "name": name,
                "ok": bool(report["tools"][name]),
                "fix": "" if report["tools"][name] else "Install " + name + " and add it to PATH",
            }
        )
    for name, model in (("server", ServerConfig), ("runner", RunnerConfig)):
        path = directory / (name + ".json")
        if not path.exists():
            checks.append(
                {
                    "name": name + "_config",
                    "ok": False,
                    "fix": "Run webcodex init on this machine, or install the matching configuration",
                }
            )
            continue
        try:
            # Keep typed parse/validation errors for the redacted diagnostics below.
            model.model_validate(_json(path))
            checks.append({"name": name + "_config", "ok": True})
        except (OSError, ValueError) as error:
            checks.append(
                {
                    "name": name + "_config",
                    "ok": False,
                    **_configuration_error(error),
                    "fix": "Correct the reported fields or JSON position",
                }
            )
    path = directory / "client.json"
    if path.exists():
        try:
            client_config = _json(path)
            if (
                not isinstance(client_config.get("server_url"), str)
                or not isinstance(client_config.get("token"), str)
                or len(client_config["token"]) < 32
            ):
                raise ValueError("Invalid client configuration")
            url = server_url(client_config["server_url"])
            with httpx.Client(base_url=url, timeout=3, trust_env=False) as client:
                health = client.get("/healthz")
                checks.append(
                    {
                        "name": "server_connection",
                        "ok": health.status_code == 200,
                        "http_status": health.status_code,
                    }
                )
                auth = client.post(
                    "/api/call",
                    headers={"Authorization": "Bearer " + client_config["token"]},
                    json={"name": "projects_list", "arguments": {}},
                )
                checks.append(
                    {
                        "name": "client_authentication",
                        "ok": auth.status_code == 200,
                        "http_status": auth.status_code,
                    }
                )
                if auth.status_code == 200:
                    payload = auth.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("projects", []), list
                    ):
                        raise ValueError("Invalid project response")
                    projects = payload.get("projects", [])
                    report["projects"] = {
                        "count": len(projects),
                        "online": sum(p.get("online") is True for p in projects),
                    }
                    checks.append(
                        {
                            "name": "runner_connection",
                            "ok": any(p.get("online") for p in projects),
                            "fix": "Start the registered Runner"
                            if not any(p.get("online") for p in projects)
                            else "",
                        }
                    )
        except (OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError) as error:
            checks.append(
                {
                    "name": "server_connection",
                    "ok": False,
                    "error": type(error).__name__,
                    "fix": "Check the Server URL, listening port and service status",
                }
            )
    else:
        checks.append(
            {"name": "client_config", "ok": False, "fix": "Create or install client.json"}
        )
    if os.name == "nt":
        report["semantic_desktop"] = {
            "backend": "UIAutomation",
            "powershell": bool(shutil.which("powershell.exe")),
            "assemblies": [
                "UIAutomationClient",
                "UIAutomationTypes",
                "UIAutomationClientsideProviders",
            ],
        }
    elif sys.platform == "linux":
        report["semantic_desktop"] = {
            "backend": "AT-SPI",
            "system_python": Path("/usr/bin/python3").exists(),
            "session_bus": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
            "required_packages": ["python3-gi", "gir1.2-atspi-2.0", "at-spi2-core"],
        }
    probe = None
    if os.name == "nt" and shutil.which("powershell.exe"):
        probe = [
            shutil.which("powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; Add-Type -AssemblyName UIAutomationClient; Add-Type -AssemblyName UIAutomationTypes; Add-Type -AssemblyName UIAutomationClientsideProviders",
        ]
    elif sys.platform == "linux" and Path("/usr/bin/python3").exists():
        probe = [
            "/usr/bin/python3",
            "-c",
            "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi",
        ]
    if probe:
        try:
            result = subprocess.run(probe, capture_output=True, timeout=5, check=False)
            report["semantic_desktop"]["dependencies_available"] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            report["semantic_desktop"]["dependencies_available"] = False
    report["update"] = update_check(directory)
    if share:
        # Only allowlisted scalar observations leave the machine. No raw errors/configuration/output.
        report["python_executable"] = "<python>"
        report["config_dir"] = "<config>"
        report["tools"] = {key: bool(value) for key, value in report["tools"].items()}
        report["computer"] = {
            key: value
            for key, value in report["computer"].items()
            if key in {"available", "platform", "backend"}
            and isinstance(value, (bool, int, type(None)))
        }
        report["update"] = {
            "runner_restart_ready": report["update"]["runner_restart_ready"],
            "running_process_count": len(report["update"]["running_processes"]),
            "running_delivery_count": len(report["update"]["running_deliveries"]),
            "errors": report["update"]["errors"],
        }
        report["shareable"] = True
    return report


def backup(directory: Path, destination: Path | None = None) -> dict:
    directory = directory.expanduser().resolve()
    destination = destination or directory / "backups" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    destination = destination.expanduser().absolute()
    if destination.exists():
        raise ValueError("Backup destination already exists")
    private_dir(destination)
    files = []
    for filename in ("server.json", "runner.json", "client.json", "retention.json"):
        source = directory / filename
        if source.exists():
            if is_link(source):
                raise ValueError("Configuration files must not be links")
            target = destination / filename
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)
            files.append(filename)
    databases = []
    for name, model, suffixes in (
        ("server", ServerConfig, ("state.sqlite3",)),
        ("runner", RunnerConfig, ("deliveries.sqlite3", "processes/processes.sqlite3")),
    ):
        config_path = directory / (name + ".json")
        if not config_path.exists():
            continue
        config = load_config(config_path, model)
        for suffix in suffixes:
            source = config.state_dir / suffix
            if source.exists():
                target = destination / name / suffix
                private_dir(target.parent)
                with _readonly(source) as src, sqlite3.connect(target) as dest:
                    src.backup(dest)
                target.chmod(0o600)
                databases.append(str(target.relative_to(destination)))
    manifest = {
        "created_at": time.time(),
        "version": __version__,
        "files": files,
        "databases": databases,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return {"backup": str(destination), **manifest}


def _completed_tasks(db) -> set[str]:
    return {
        r[0]
        for r in db.execute(
            "SELECT t.id FROM tasks t WHERE t.status='completed' "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.task_id=t.id AND j.status IN ('queued','running','pending_approval','unknown')) "
            "AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.task_id=t.id AND p.status='succeeded' "
            "AND p.process_id IS NOT NULL AND p.process_status='running' "
            "AND NOT EXISTS (SELECT 1 FROM jobs n WHERE n.task_id=p.task_id AND n.process_id=p.process_id "
            "AND n.status='succeeded' AND (n.updated>p.updated OR (n.updated=p.updated AND n.id>p.id))))"
        )
    }


def _compact_payload(raw: str, when: float) -> str:
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("retention"):
        return raw
    kept = {
        key: value[key]
        for key in (
            "process_id",
            "agent_id",
            "status",
            "exit_code",
            "outcome",
            "cursor",
            "next_cursor",
            "truncated",
            "total_bytes",
            "registered",
            "project",
            "error_type",
        )
        if key in value
    }
    if value.get("error"):
        kept["error"] = str(value["error"])[:2000]
    kept["retention"] = {"payload_removed_at": when, "original_bytes": len(raw.encode("utf-8"))}
    kept["has_more"] = False
    kept["output"] = "Output and attachments removed by the retention policy."
    if "mcp_result" in value:
        kept["mcp_result"] = {"content": [{"type": "text", "text": kept["output"]}]}
    return json.dumps(kept, ensure_ascii=False)


def retention(
    directory: Path,
    *,
    days: int | None = None,
    max_mib: int | None = None,
    apply: bool = False,
    save_policy: bool = False,
) -> dict:
    directory = directory.expanduser().resolve()
    policy_path = directory / "retention.json"
    policy = _json(policy_path) if policy_path.exists() else {"days": 30, "max_mib": 1024}
    policy = {
        "days": policy.get("days", 30) if days is None else days,
        "max_mib": policy.get("max_mib", 1024) if max_mib is None else max_mib,
    }
    if (
        type(policy["days"]) is not int
        or not 1 <= policy["days"] <= 3650
        or type(policy["max_mib"]) is not int
        or not 1 <= policy["max_mib"] <= 1048576
    ):
        raise ValueError("days must be 1–3650 and max_mib 1–1048576")
    if save_policy:
        if is_link(policy_path):
            raise ValueError("Retention policy must not be a link")
        private_dir(directory)
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        policy_path.chmod(0o600)
    now = time.time()
    config = load_config(directory / "runner.json", RunnerConfig)
    logs = config.state_dir / "processes"
    candidates, protected = [], 0
    process_db = logs / "processes.sqlite3"
    live_task_ids = set()
    if process_db.exists():
        with _readonly(process_db) as db:
            live_task_ids = {
                r[0]
                for r in db.execute("SELECT DISTINCT task_id FROM processes WHERE status='running'")
            }
    server_config = directory / "server.json"
    server_db = (
        load_config(server_config, ServerConfig).state_dir / "state.sqlite3"
        if server_config.exists()
        else None
    )
    completed = set()
    if server_db and server_db.exists():
        with _readonly(server_db) as db:
            completed = _completed_tasks(db) - live_task_ids
            for jid, tid, raw, updated in db.execute(
                "SELECT id,task_id,result,updated FROM jobs WHERE result IS NOT NULL"
            ):
                original_size = len(raw.encode("utf-8"))
                if tid not in completed:
                    protected += original_size
                    continue
                try:
                    compact = _compact_payload(raw, now)
                except (ValueError, TypeError):
                    protected += original_size
                    continue
                saved = original_size - len(compact.encode("utf-8"))
                if saved > 0:
                    candidates.append(
                        {
                            "kind": "job_payload",
                            "id": jid,
                            "task_id": tid,
                            "bytes": saved,
                            "time": updated,
                            "digest": hashlib.sha256(raw.encode("utf-8")).digest(),
                        }
                    )
    if process_db.exists():
        with _readonly(process_db) as db:
            for pid, tid, status, finished in db.execute(
                "SELECT id,task_id,status,finished FROM processes"
            ):
                if not re.fullmatch(r"[0-9a-f]{32}", pid):
                    continue
                path = logs / (pid + ".log")
                if not path.is_file() or is_link(path):
                    continue
                if status == "running" or not finished or (server_db and tid not in completed):
                    protected += path.stat().st_size
                    continue
                candidates.append(
                    {
                        "path": path,
                        "bytes": path.stat().st_size,
                        "time": finished,
                        "kind": "process_output",
                        "id": pid,
                        "task_id": tid,
                    }
                )
    # Only artifacts owned by completed tasks. Profiles and installation files are excluded.
    extensions = config.state_dir / "extensions"
    if extensions.exists() and not is_link(extensions):
        for artifacts in extensions.glob("*/sessions/*/*/*/artifacts"):
            tid = artifacts.parent.name
            if tid not in completed or any(
                is_link(parent)
                for parent in [artifacts, *artifacts.parents]
                if parent != config.state_dir.parent
            ):
                continue
            for current, directories, files in os.walk(artifacts, followlinks=False):
                directories[:] = [name for name in directories if not is_link(Path(current) / name)]
                for name in files:
                    path = Path(current) / name
                    if is_link(path) or not path.is_file():
                        continue
                    stat = path.stat()
                    candidates.append(
                        {
                            "path": path,
                            "bytes": stat.st_size,
                            "time": stat.st_mtime,
                            "kind": "artifact",
                            "id": str(path.relative_to(config.state_dir)),
                            "task_id": tid,
                        }
                    )
    candidates.sort(key=lambda c: c["time"])
    total = protected + sum(c["bytes"] for c in candidates)
    target_bytes = policy["max_mib"] * 1024 * 1024
    cutoff = now - policy["days"] * 86400
    chosen, remaining = [], total
    for item in candidates:
        if item["time"] < cutoff or remaining > target_bytes:
            chosen.append(item)
            remaining -= item["bytes"]
    removed, reclaimed, skipped = 0, 0, []
    pruned_jobs = {}
    if apply:
        if server_db and server_db.exists():
            with sqlite3.connect(server_db, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                current_completed = _completed_tasks(db)
                for item in [c for c in chosen if c["kind"] == "job_payload"]:
                    if item["task_id"] not in current_completed:
                        skipped.append(item["id"])
                        continue
                    row = db.execute("SELECT result FROM jobs WHERE id=?", (item["id"],)).fetchone()
                    if (
                        not row
                        or not isinstance(row[0], str)
                        or hashlib.sha256(row[0].encode("utf-8")).digest() != item["digest"]
                    ):
                        skipped.append(item["id"])
                        continue
                    compact = _compact_payload(row[0], now)
                    changed = db.execute(
                        "UPDATE jobs SET result=? WHERE id=? AND result=?",
                        (compact, item["id"], row[0]),
                    ).rowcount
                    if changed:
                        reclaimed += item["bytes"]
                        pruned_jobs[item["id"]] = compact
                        db.execute(
                            "INSERT INTO events(task_id,kind,data,created) VALUES (?,?,?,?)",
                            (
                                item["task_id"],
                                "retention.payload_removed",
                                json.dumps({"job_id": item["id"], "bytes": item["bytes"]}),
                                now,
                            ),
                        )
        for item in [c for c in chosen if c["kind"] != "job_payload"]:
            if server_db and server_db.exists():
                with _readonly(server_db) as db:
                    if item["task_id"] not in _completed_tasks(db):
                        skipped.append(item["id"])
                        continue
            if item["kind"] == "process_output":
                with _readonly(process_db) as db:
                    row = db.execute(
                        "SELECT status,finished FROM processes WHERE id=?", (item["id"],)
                    ).fetchone()
                    if not row or row[0] == "running" or not row[1]:
                        skipped.append(item["id"])
                        continue
            if is_link(item["path"]):
                skipped.append(item["id"])
                continue
            try:
                if item["path"].stat().st_mtime > now:
                    skipped.append(item["id"])
                    continue
                item["path"].unlink()
                removed += item["bytes"]
            except FileNotFoundError:
                skipped.append(item["id"])
        delivery_db = config.state_dir / "deliveries.sqlite3"
        if pruned_jobs and delivery_db.exists() and not is_link(delivery_db):
            with sqlite3.connect(delivery_db, timeout=5) as db:
                for jid, compact in pruned_jobs.items():
                    old = db.execute(
                        "SELECT result FROM deliveries WHERE id=? AND acknowledged=1 AND status IN ('succeeded','failed')",
                        (jid,),
                    ).fetchone()
                    if old and old[0]:
                        saved = len(old[0].encode("utf-8")) - len(compact.encode("utf-8"))
                        if saved > 0:
                            db.execute(
                                "UPDATE deliveries SET result=? WHERE id=? AND acknowledged=1 AND status IN ('succeeded','failed')",
                                (compact, jid),
                            )
                            reclaimed += saved
    return {
        "policy": policy,
        "dry_run": not apply,
        "eligible_bytes": total - protected,
        "protected_bytes": protected,
        "selected_bytes": sum(c["bytes"] for c in chosen),
        "removed_file_bytes": removed,
        "reclaimed_payload_bytes": reclaimed,
        "capacity_blocked_by_protected_data": protected > target_bytes,
        "files": [{"kind": c["kind"], "id": c["id"], "bytes": c["bytes"]} for c in chosen],
        "skipped": skipped,
    }
