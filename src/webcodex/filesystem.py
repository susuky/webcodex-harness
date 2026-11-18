from __future__ import annotations

import contextlib
import fnmatch
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from .config import is_link, project_binding

MAX_FILE = 2 * 1024 * 1024
SKIP_DIRS = {".git", ".webcodex", ".venv", "node_modules", "__pycache__", "target", "dist"}


class Workspace:
    """Directory-fd file operations; symlinks and repository metadata are never traversed."""

    def __init__(
        self, root: Path, expected_binding: str | None = None, *, parent: Workspace | None = None
    ):
        self.root = root.absolute()
        self.parent_workspace = parent
        self.identity = None
        with self.directory() as directory:
            info = self.directory_info(directory)
            self.identity = (info.st_dev, info.st_ino)
            self.binding = project_binding(self.root, info=info)
            if expected_binding is not None and expected_binding != self.binding:
                raise ValueError("Project directory binding changed")

    def child(self, path: str) -> Workspace:
        parts = self.parts(path, root_ok=True)
        return Workspace(self.root.joinpath(*parts), parent=self)

    @staticmethod
    def directory_info(directory) -> os.stat_result:
        return directory.stat() if os.name == "nt" else os.fstat(directory)

    def check_root(self, directory):
        info = self.directory_info(directory)
        if self.identity is not None and (info.st_dev, info.st_ino) != self.identity:
            raise ValueError("Project directory binding changed")

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
    def root_directory(self):
        if self.parent_workspace is not None:
            relative = self.root.relative_to(self.parent_workspace.root).as_posix()
            with self.parent_workspace.directory(relative) as directory:
                self.check_root(directory)
                yield directory
            return
        if os.name == "nt":
            from .windows import pinned

            # Pin every ancestor, including those above the registered root. Windows
            # denies renaming pinned directories; OPEN_REPARSE_POINT checks each hop.
            with contextlib.ExitStack() as stack:
                current = Path(self.root.anchor)
                stack.enter_context(pinned(current, directory=True))
                for part in self.root.parts[1:]:
                    current /= part
                    stack.enter_context(pinned(current, directory=True))
                self.check_root(current)
                yield current
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.root.anchor, flags)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            self.check_root(fd)
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def directory(self, path: str = ".", *, create: bool = False):
        parts = self.parts(path, root_ok=True)
        with self.root_directory() as root:
            if os.name == "nt":
                from .windows import pinned

                with contextlib.ExitStack() as stack:
                    current = root
                    for part in parts:
                        current /= part
                        if create:
                            current.mkdir(exist_ok=True)
                        stack.enter_context(pinned(current, directory=True))
                    yield current
                return
            fd = os.dup(root)
            try:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                for part in parts:
                    if create:
                        try:
                            os.mkdir(part, dir_fd=fd)
                        except FileExistsError:
                            pass
                    child = os.open(part, flags, dir_fd=fd)
                    os.close(fd)
                    fd = child
                yield fd
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def parent(self, path: str, *, create: bool = False):
        parts = self.parts(path)
        with self.directory("/".join(parts[:-1]) or ".", create=create) as directory:
            yield directory, parts[-1]

    def path(self, path: str, *, directory: bool = False) -> Path:
        parts = self.parts(path, root_ok=directory)
        with self.directory():
            pass
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
        with self.open_file(path) as stream:
            self.file_info(stream, maximum)
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise ValueError(f"File exceeds the {maximum}-byte read limit")
            return content

    @contextlib.contextmanager
    def open_file(self, path: str):
        with self.parent(path) as (parent, name):
            if os.name == "nt":
                import msvcrt

                import win32api
                import win32con

                from .windows import pinned

                with pinned(parent / name, directory=False) as handle:
                    process = win32api.GetCurrentProcess()
                    duplicate = win32api.DuplicateHandle(
                        process, handle, process, 0, False, win32con.DUPLICATE_SAME_ACCESS
                    )
                    try:
                        fd = msvcrt.open_osfhandle(int(duplicate), os.O_RDONLY | os.O_BINARY)
                    except BaseException:
                        duplicate.Close()
                        raise
                    duplicate.Detach()  # The CRT fd now owns this duplicate, not pinned's handle.
                    with os.fdopen(fd, "rb") as stream:
                        yield stream
                return
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                yield stream

    @staticmethod
    def file_info(stream, maximum: int | None = None) -> os.stat_result:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Only regular, single-link files can be read")
        if maximum is not None and info.st_size > maximum:
            raise ValueError(f"File exceeds the {maximum}-byte read limit")
        return info

    def copy_file(
        self, path: str, target: Path, maximum: int, *, preserve_mode: bool = True
    ) -> int:
        with self.open_file(path) as stream:
            info = self.file_info(stream, maximum)
            total = 0
            with target.open("xb") as output:
                while chunk := stream.read(min(65536, maximum - total + 1)):
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError(f"File exceeds the {maximum}-byte copy limit")
                    output.write(chunk)
            if preserve_mode:
                target.chmod(stat.S_IMODE(info.st_mode) & 0o777)
            return total

    def walk(
        self, path: str = ".", *, skip_dirs: set[str] = SKIP_DIRS, skip_unavailable: bool = False
    ):
        relative = PurePosixPath(*self.parts(path, root_ok=True))
        with self.directory(path) as directory:
            with os.scandir(directory) as entries:
                items = []
                for entry in entries:
                    try:
                        # Windows DirEntry.stat leaves identity and link counts at zero.
                        info = (
                            os.stat(directory / entry.name, follow_symlinks=False)
                            if os.name == "nt"
                            else entry.stat(follow_symlinks=False)
                        )
                        items.append((entry.name, info))
                    except (FileNotFoundError, PermissionError):
                        if not skip_unavailable:
                            raise
            items.sort(key=lambda item: (stat.S_ISDIR(item[1].st_mode), item[0]))
            for leaf, info in items:
                name = (relative / leaf).as_posix()
                if stat.S_ISDIR(info.st_mode) and leaf.casefold() in skip_dirs:
                    continue
                yield name, info
                if (
                    stat.S_ISDIR(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    try:
                        yield from self.walk(
                            name, skip_dirs=skip_dirs, skip_unavailable=skip_unavailable
                        )
                    except (FileNotFoundError, PermissionError):
                        if not skip_unavailable:
                            raise

    def write(self, path: str, content: bytes, *, maximum: int = MAX_FILE) -> dict:
        if len(content) > maximum:
            raise ValueError("File exceeds the write limit")
        with self.parent(path, create=True) as (parent, name):
            windows = os.name == "nt"
            options = {} if windows else {"dir_fd": parent}
            target = parent / name if windows else name
            mode = 0o644
            try:
                with self.open_file(path) as stream:
                    mode = stat.S_IMODE(self.file_info(stream).st_mode) & 0o777
            except FileNotFoundError:
                pass
            temporary = ".webcodex-write-" + uuid.uuid4().hex
            if windows:
                temporary = parent / temporary
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, **options)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                replace_options = {} if windows else {"src_dir_fd": parent, "dst_dir_fd": parent}
                os.replace(temporary, target, **replace_options)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, **options)
        return {"path": path, "bytes": len(content)}

    def delete(self, path: str) -> dict:
        with self.parent(path) as (parent, name):
            with self.open_file(path) as stream:
                self.file_info(stream)
            if os.name == "nt":
                os.unlink(parent / name)
            else:
                os.unlink(name, dir_fd=parent)
        return {"path": path, "deleted": True}

    def files(self, path: str, pattern: str, offset: int, limit: int) -> dict:
        result, seen = [], 0
        with contextlib.closing(self.walk(path, skip_unavailable=True)) as entries:
            for relative, info in entries:
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    continue
                try:
                    self.parts(relative)
                except ValueError:
                    continue
                if not fnmatch.fnmatch(relative, pattern):
                    continue
                if seen >= offset:
                    if len(result) == limit:
                        return {"files": result, "next_offset": seen}
                    result.append({"path": relative, "bytes": info.st_size})
                seen += 1
        return {"files": result, "next_offset": None}
