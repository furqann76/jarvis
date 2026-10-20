"""Filesystem tools. Every path is normalized and re-checked against the SecurityPolicy
inside the tool, on top of the pre-execution check in security.validation."""

import asyncio
import fnmatch
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.security.permissions import SecurityPolicy, normalize_path
from jarvis.tools.base import BaseTool

MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 500
MAX_READ_BYTES = 100_000


class PathAccessError(Exception):
    pass


class FileTool(BaseTool):
    def __init__(self, security_policy: SecurityPolicy):
        self.security = security_policy

    def _resolve(self, raw: str) -> Path:
        resolved = normalize_path(raw)
        if not self.security.is_path_allowed(resolved):
            raise PathAccessError(f"Access denied to path: {resolved}")
        return resolved

    def _resolve_mutable(self, raw: str) -> Path:
        """Resolve a path that is about to be moved/trashed/deleted.

        Only the parent is resolved, so a symlink is acted on as the link itself rather
        than its target. Sandbox root directories themselves are never mutable.
        """
        expanded = Path(os.path.expanduser(raw))
        if expanded.name in ("", ".", ".."):
            resolved = normalize_path(expanded)
        else:
            resolved = normalize_path(expanded.parent) / expanded.name
        if not self.security.is_path_allowed(resolved):
            raise PathAccessError(f"Access denied to path: {resolved}")
        if resolved in self.security.allowed_roots:
            raise PathAccessError(f"Refusing to modify a sandbox root directory: {resolved}")
        return resolved

    @staticmethod
    def _fail(tool_call_id: str, error: str, data: Any = None) -> ToolResult:
        return ToolResult(tool_call_id=tool_call_id, success=False, error=error, data=data)


def _entry_info(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        modified = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        if path.is_dir():
            # Folders report how many items they hold; a bare size of 0 made models
            # describe non-empty folders as "empty".
            try:
                count = sum(1 for _ in os.scandir(path))
            except OSError:
                count = None
            return {"name": path.name, "type": "folder", "item_count": count,
                    "modified": modified}
        return {"name": path.name, "type": "file", "size_bytes": st.st_size,
                "modified": modified}
    except OSError:
        return {"name": path.name, "type": "broken link"}


# --------------------------------------------------------------------------- Tier 0


class ListDirectoryTool(FileTool):
    name = "list_directory"
    description = "List files and directories within an allowed folder path."
    risk_level = RiskLevel.SAFE
    path_arguments = ("directory_path",)
    parameters = {
        "type": "object",
        "properties": {
            "directory_path": {
                "type": "string",
                "description": "Absolute or home-relative (~/...) path to inspect.",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "Include entries starting with '.'. Defaults to false.",
            },
        },
        "required": ["directory_path"],
    }

    async def execute(
        self, tool_call_id: str, directory_path: str, show_hidden: bool = False
    ) -> ToolResult:
        try:
            resolved = self._resolve(directory_path)
            if not resolved.is_dir():
                return self._fail(tool_call_id, f"Not a directory: {resolved}")
            names = sorted(os.listdir(resolved), key=str.lower)
            if not show_hidden:
                names = [n for n in names if not n.startswith(".")]
            truncated = len(names) > MAX_LIST_ENTRIES
            items = [_entry_info(resolved / n) for n in names[:MAX_LIST_ENTRIES]]
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"path": str(resolved), "items": items, "truncated": truncated},
            )
        except (PathAccessError, OSError) as e:
            return self._fail(tool_call_id, str(e))

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class SearchFilesTool(FileTool):
    name = "search_files"
    description = (
        "Find files whose name matches a glob pattern (case-insensitive, e.g. '*.pdf' or "
        "'*invoice*') inside an allowed directory, searching subdirectories by default."
    )
    risk_level = RiskLevel.SAFE
    path_arguments = ("directory_path",)
    parameters = {
        "type": "object",
        "properties": {
            "directory_path": {"type": "string", "description": "Directory to search in."},
            "pattern": {"type": "string", "description": "Filename glob pattern."},
            "recursive": {
                "type": "boolean",
                "description": "Search subdirectories (default true).",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_RESULTS,
                "description": "Maximum number of matches to return (default 100).",
            },
        },
        "required": ["directory_path", "pattern"],
    }

    async def execute(
        self,
        tool_call_id: str,
        directory_path: str,
        pattern: str,
        recursive: bool = True,
        max_results: int = 100,
    ) -> ToolResult:
        try:
            root = self._resolve(directory_path)
            if not root.is_dir():
                return self._fail(tool_call_id, f"Not a directory: {root}")
            matches, truncated = await asyncio.to_thread(
                self._search, root, pattern.lower(), recursive, max_results
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"path": str(root), "pattern": pattern, "matches": matches,
                      "truncated": truncated},
            )
        except (PathAccessError, OSError) as e:
            return self._fail(tool_call_id, str(e))

    def _search(
        self, root: Path, pattern: str, recursive: bool, max_results: int
    ) -> tuple[list[str], bool]:
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            # Prune subtrees the policy denies (e.g. a deny pattern inside an allowed root).
            dirnames[:] = [d for d in dirnames if self.security.is_path_allowed(current / d)]
            for name in sorted(dirnames + filenames, key=str.lower):
                if fnmatch.fnmatch(name.lower(), pattern):
                    candidate = current / name
                    if self.security.is_path_allowed(candidate):
                        matches.append(str(candidate))
                        if len(matches) >= max_results:
                            return matches, True
            if not recursive:
                break
        return matches, False

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class ReadTextFileTool(FileTool):
    name = "read_text_file"
    description = "Read the contents of a text file inside an allowed directory."
    risk_level = RiskLevel.SAFE
    path_arguments = ("file_path",)
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path of the file to read."},
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_BYTES,
                "description": "Maximum bytes to read (default 20000).",
            },
        },
        "required": ["file_path"],
    }

    async def execute(
        self, tool_call_id: str, file_path: str, max_bytes: int = 20_000
    ) -> ToolResult:
        try:
            resolved = self._resolve(file_path)
            if not resolved.is_file():
                return self._fail(tool_call_id, f"Not a regular file: {resolved}")
            with open(resolved, "rb") as fh:
                raw = fh.read(max_bytes + 1)
            if b"\x00" in raw:
                return self._fail(tool_call_id, f"File appears to be binary: {resolved}")
            truncated = len(raw) > max_bytes
            text = raw[:max_bytes].decode("utf-8", errors="replace")
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"path": str(resolved), "content": text, "truncated": truncated},
            )
        except (PathAccessError, OSError) as e:
            return self._fail(tool_call_id, str(e))

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


# --------------------------------------------------------------------------- Tier 1


class CreateDirectoryTool(FileTool):
    name = "create_directory"
    description = "Create a directory (including missing parents) inside an allowed directory."
    risk_level = RiskLevel.LOW
    path_arguments = ("directory_path",)
    parameters = {
        "type": "object",
        "properties": {
            "directory_path": {"type": "string", "description": "Directory to create."},
        },
        "required": ["directory_path"],
    }

    async def execute(self, tool_call_id: str, directory_path: str) -> ToolResult:
        try:
            resolved = self._resolve(directory_path)
            existed = resolved.is_dir()
            resolved.mkdir(parents=True, exist_ok=True)
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"path": str(resolved), "already_existed": existed},
            )
        except (PathAccessError, OSError) as e:
            return self._fail(tool_call_id, str(e))

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success or not execution_result.data:
            return False
        return Path(execution_result.data["path"]).is_dir()


class TrashFileTool(FileTool):
    name = "trash_file"
    description = (
        "Move a file or directory to the desktop Trash (recoverable). Prefer this over "
        "permanent deletion."
    )
    risk_level = RiskLevel.LOW
    path_arguments = ("path",)
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File or directory to trash."}},
        "required": ["path"],
    }

    def __init__(self, security_policy: SecurityPolicy, trash_dir: str | os.PathLike):
        super().__init__(security_policy)
        self.trash_dir = Path(os.path.expanduser(os.fspath(trash_dir)))

    async def execute(self, tool_call_id: str, path: str) -> ToolResult:
        try:
            resolved = self._resolve_mutable(path)
            if not resolved.exists() and not resolved.is_symlink():
                return self._fail(tool_call_id, f"No such file or directory: {resolved}")
            trashed = await asyncio.to_thread(self._trash, resolved)
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"original_path": str(resolved), "trashed_path": str(trashed)},
            )
        except (PathAccessError, OSError) as e:
            return self._fail(tool_call_id, str(e))

    def _trash(self, path: Path) -> Path:
        """Freedesktop.org home-trash implementation (files/ + info/*.trashinfo)."""
        files_dir = self.trash_dir / "files"
        info_dir = self.trash_dir / "info"
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)

        stem, suffix = path.stem, path.suffix
        counter = 1
        while True:
            name = path.name if counter == 1 else f"{stem}.{counter}{suffix}"
            info_path = info_dir / f"{name}.trashinfo"
            try:
                fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                counter += 1
                continue
            if (files_dir / name).exists():
                os.close(fd)
                os.unlink(info_path)
                counter += 1
                continue
            break

        deletion_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"[Trash Info]\nPath={quote(str(path))}\nDeletionDate={deletion_date}\n")
        target = files_dir / name
        try:
            shutil.move(str(path), str(target))
        except OSError:
            info_path.unlink(missing_ok=True)
            raise
        return target

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success or not execution_result.data:
            return False
        original = Path(execution_result.data["original_path"])
        trashed = Path(execution_result.data["trashed_path"])
        return not original.exists() and (trashed.exists() or trashed.is_symlink())


# --------------------------------------------------------------------------- Tier 2


class BulkMoveFilesTool(FileTool):
    name = "bulk_move_files"
    description = (
        "Move one or more files or directories into a destination directory. Never "
        "overwrites existing items."
    )
    risk_level = RiskLevel.HIGH
    path_arguments = ("source_paths", "destination_directory")
    parameters = {
        "type": "object",
        "properties": {
            "source_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Files or directories to move.",
            },
            "destination_directory": {
                "type": "string",
                "description": "Existing directory to move the items into.",
            },
        },
        "required": ["source_paths", "destination_directory"],
    }

    def describe_call(self, arguments: dict[str, Any]) -> str:
        sources = arguments.get("source_paths", [])
        return f"Move {len(sources)} item(s) into {arguments.get('destination_directory')}"

    async def execute(
        self, tool_call_id: str, source_paths: list[str], destination_directory: str
    ) -> ToolResult:
        try:
            dest = self._resolve(destination_directory)
            if not dest.is_dir():
                return self._fail(tool_call_id, f"Destination is not a directory: {dest}")
            sources = [self._resolve_mutable(p) for p in source_paths]
        except PathAccessError as e:
            return self._fail(tool_call_id, str(e))

        moved: list[dict[str, str]] = []
        errors: list[str] = []
        for src in sources:
            target = dest / src.name
            if not src.exists():
                errors.append(f"No such file or directory: {src}")
            elif target.exists():
                errors.append(f"Refusing to overwrite existing item: {target}")
            elif dest == src or dest.is_relative_to(src):
                errors.append(f"Cannot move a directory into itself: {src}")
            else:
                try:
                    await asyncio.to_thread(shutil.move, str(src), str(target))
                    moved.append({"from": str(src), "to": str(target)})
                except OSError as e:
                    errors.append(f"{src}: {e}")
        data = {"moved": moved, "errors": errors}
        if errors:
            return self._fail(tool_call_id, "; ".join(errors), data=data)
        return ToolResult(tool_call_id=tool_call_id, success=True, data=data)

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success or not execution_result.data:
            return False
        return all(
            Path(m["to"]).exists() and not Path(m["from"]).exists()
            for m in execution_result.data["moved"]
        )


class PermanentlyDeleteFilesTool(FileTool):
    name = "permanently_delete_files"
    description = (
        "Permanently delete files or directories. This cannot be undone; use trash_file "
        "unless the user explicitly asks for permanent deletion."
    )
    risk_level = RiskLevel.HIGH
    path_arguments = ("paths",)
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Files or directories to delete.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Required to delete non-empty directories (default false).",
            },
        },
        "required": ["paths"],
    }

    def describe_call(self, arguments: dict[str, Any]) -> str:
        paths = arguments.get("paths", [])
        rec = " (recursive)" if arguments.get("recursive") else ""
        return f"Permanently delete {len(paths)} item(s){rec}: " + ", ".join(paths)

    async def execute(
        self, tool_call_id: str, paths: list[str], recursive: bool = False
    ) -> ToolResult:
        try:
            targets = [self._resolve_mutable(p) for p in paths]
        except PathAccessError as e:
            return self._fail(tool_call_id, str(e))

        deleted: list[str] = []
        errors: list[str] = []
        for target in targets:
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    if recursive:
                        await asyncio.to_thread(shutil.rmtree, target)
                    else:
                        target.rmdir()
                else:
                    errors.append(f"No such file or directory: {target}")
                    continue
                deleted.append(str(target))
            except OSError as e:
                errors.append(f"{target}: {e}")
        data = {"deleted": deleted, "errors": errors}
        if errors:
            return self._fail(tool_call_id, "; ".join(errors), data=data)
        return ToolResult(tool_call_id=tool_call_id, success=True, data=data)

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success or not execution_result.data:
            return False
        return all(
            not Path(p).exists() and not Path(p).is_symlink()
            for p in execution_result.data["deleted"]
        )


def build_file_tools(policy: SecurityPolicy, trash_dir: str | os.PathLike) -> list[BaseTool]:
    return [
        ListDirectoryTool(policy),
        SearchFilesTool(policy),
        ReadTextFileTool(policy),
        CreateDirectoryTool(policy),
        TrashFileTool(policy, trash_dir),
        BulkMoveFilesTool(policy),
        PermanentlyDeleteFilesTool(policy),
    ]
