from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from .config import is_link

MAX_FILE = 2 * 1024 * 1024
SKIP_DIRS = {".git", ".webcodex", ".venv", "node_modules", "__pycache__", "target", "dist"}


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Workspace:
    """Directory-fd file operations; symlinks and repository metadata are never traversed."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def parts(self, path: str, *, root_ok: bool = False) -> tuple[str, ...]:
        if not path or "\x00" in path or "\\" in path:
            raise ValueError("Use a nonempty relative POSIX path")
        value = PurePosixPath(path)
        if os.name == "nt":
            reserved = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
                f"{prefix}{n}" for prefix in ["com", "lpt"] for n in "123456789¹²³"
            }
            for part in value.parts:
                if (
                    any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                    or part.endswith((".", " "))
                    or part.split(".")[0].rstrip(" ").casefold() in reserved
                    or part.casefold() in {".git", ".webcodex"}
                ):
                    raise ValueError("Invalid or reserved Windows workspace path")
        if (
            value.is_absolute()
            or ".." in value.parts
            or ".git" in value.parts
            or ".webcodex" in value.parts
        ):
            raise ValueError("Path is outside the permitted workspace or targets private metadata")
        if not value.parts and not root_ok:
            raise ValueError("A file path is required")
        return value.parts

    @contextlib.contextmanager
    def parent(self, path: str, *, create: bool = False):
        parts = self.parts(path)
        if os.name == "nt":
            from .windows import pinned

            # Pin every ancestor, including those above the registered root. Windows
            # denies renaming pinned directories; OPEN_REPARSE_POINT checks each hop.
            with contextlib.ExitStack() as stack:
                current = Path(self.root.anchor)
                stack.enter_context(pinned(current, directory=True))
                for index, part in enumerate((*self.root.parts[1:], *parts[:-1])):
                    current /= part
                    if create and index >= len(self.root.parts) - 1:
                        current.mkdir(exist_ok=True)
                    stack.enter_context(pinned(current, directory=True))
                yield current, parts[-1]
            return
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def path(self, path: str, *, directory: bool = False) -> Path:
        parts = self.parts(path, root_ok=directory)
        current = self.root
        for part in parts:
            current /= part
            if is_link(current):
                raise ValueError("Symlinks are not allowed in workspace paths")
            if os.name == "nt" and current.exists():
                from .windows import pinned

                with pinned(current, directory=current.is_dir()):
                    pass
        if directory and not current.is_dir():
            raise ValueError("Working directory does not exist")
        return current

    def read(self, path: str, maximum: int = MAX_FILE) -> bytes:
        with self.parent(path) as (parent, name):
            if os.name == "nt":
                import win32file

                from .windows import pinned

                with pinned(parent / name, directory=False) as handle:
                    if win32file.GetFileSize(handle) > maximum:
                        raise ValueError(f"File exceeds the {maximum}-byte read limit")
                    return win32file.ReadFile(handle, maximum + 1)[1]
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Only regular, single-link files can be read")
                content = stream.read(maximum + 1)
                if len(content) > maximum:
                    raise ValueError(f"File exceeds the {maximum}-byte read limit")
                return content

    def write(self, path: str, content: bytes, expected_sha256: str | None) -> dict:
        if len(content) > MAX_FILE:
            raise ValueError("File exceeds the write limit")
        with self.parent(path, create=True) as (parent, name):
            windows = os.name == "nt"
            options = {} if windows else {"dir_fd": parent}
            target = parent / name if windows else name
            mode = 0o644
            try:
                previous = self.read(path)
                info = os.stat(target, **options, follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode) & 0o777
            except FileNotFoundError:
                previous, info = None, None
            if previous is not None and expected_sha256 != sha256(previous):
                raise ValueError(
                    "File changed or expected_sha256 is missing; read it again before editing"
                )
            if previous is None and expected_sha256 is not None:
                raise ValueError("File no longer exists")
            temporary = ".webcodex-write-" + uuid.uuid4().hex
            if windows:
                temporary = parent / temporary
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, **options)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if info is None:
                    # link is an atomic create-if-absent; it cannot overwrite a concurrent new file.
                    if windows:
                        os.rename(temporary, target)  # Windows rename is create-if-absent.
                    else:
                        os.link(
                            temporary,
                            name,
                            src_dir_fd=parent,
                            dst_dir_fd=parent,
                            follow_symlinks=False,
                        )
                        os.unlink(temporary, dir_fd=parent)
                else:
                    current = os.stat(target, **options, follow_symlinks=False)
                    if (current.st_ino, current.st_mtime_ns, current.st_size) != (
                        info.st_ino,
                        info.st_mtime_ns,
                        info.st_size,
                    ):
                        raise ValueError("File changed while preparing the edit")
                    replace_options = (
                        {} if windows else {"src_dir_fd": parent, "dst_dir_fd": parent}
                    )
                    os.replace(temporary, target, **replace_options)
                if not windows:
                    os.fsync(parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, **options)
        return {"path": path, "sha256": sha256(content), "bytes": len(content)}

    def delete(self, path: str, expected_sha256: str) -> dict:
        with self.parent(path) as (parent, name):
            content = self.read(path)
            if sha256(content) != expected_sha256:
                raise ValueError("File changed; read it again before deleting")
            if os.name == "nt":
                os.unlink(parent / name)
            else:
                os.unlink(name, dir_fd=parent)
        return {"path": path, "deleted": True}

    def files(self, path: str, pattern: str, offset: int, limit: int) -> dict:
        root = self.path(path, directory=True)
        result, seen = [], 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d.casefold() not in SKIP_DIRS and not is_link(Path(directory) / d)
            )
            for name in sorted(files):
                item = Path(directory) / name
                if is_link(item) or not item.is_file() or item.stat().st_nlink != 1:
                    continue
                relative = item.relative_to(self.root).as_posix()
                try:
                    self.parts(relative)
                except ValueError:
                    continue
                if not fnmatch.fnmatch(relative, pattern):
                    continue
                if seen >= offset:
                    if len(result) == limit:
                        return {"files": result, "next_offset": seen}
                    result.append({"path": relative, "bytes": item.stat().st_size})
                seen += 1
        return {"files": result, "next_offset": None}
