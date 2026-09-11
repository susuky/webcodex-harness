"""A small MCP plugin whose observation counter demonstrates session continuity."""

import os
from collections import Counter
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("project-tools")
observations = 0


@mcp.tool()
def project_stats() -> dict[str, int | dict[str, int]]:
    """Count project files by extension and report observations in this live provider session."""
    global observations
    observations += 1
    counts = Counter()
    for directory, dirs, files in os.walk(Path.cwd(), followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in {".git", ".venv", "node_modules", "target"}
            and not (Path(directory) / d).is_symlink()
        ]
        for name in files:
            if not (Path(directory) / name).is_symlink():
                counts[Path(name).suffix or "(no extension)"] += 1
    return {
        "by_extension": dict(counts),
        "total_files": sum(counts.values()),
        "observations": observations,
    }


@mcp.resource("project://readme")
def readme() -> str:
    """Read the current project's README.md when present."""
    path = Path.cwd() / "README.md"
    if not path.is_file() or path.is_symlink():
        return "專案沒有 README.md。"
    return path.read_text(encoding="utf-8")[:32000]


@mcp.prompt()
def summarize_project(focus: str = "程式結構") -> str:
    """Prepare a short prompt for exploring the project."""
    return f"請根據真實專案檔案，整理{focus}；先讀取專案規則再操作。"


if __name__ == "__main__":
    mcp.run()
