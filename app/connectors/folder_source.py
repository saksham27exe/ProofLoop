"""A user's own folder as a verification target.

Paths are confined to the folder (no absolute paths, drive letters, ``..``),
hidden/tool directories are skipped, and the inspector refuses (INCONCLUSIVE)
folders above the file-count or size limit instead of checking them partially.
Backups are a zip of the folder taken right before a run.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path
from typing import Any

from app.connectors.sqlite_source import SourceConfig
from app.environments.base import (
    EntitySpec,
    Environment,
    EnvironmentSchema,
    FieldSpec,
    InspectorUnavailable,
    Snapshot,
    StateInspector,
    Toolbox,
    ToolResult,
    ToolSpec,
)
from app.utils.security import UnsafePathError, normalize_relative_path, safe_join

SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
MAX_FILES = 5000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_INLINE = 256 * 1024
MAX_WRITE = 2 * 1024 * 1024

FOLDER_TOOLS = [
    ToolSpec(name="list_files", description="List files in the folder (relative paths and sizes).", mutating=False,
             parameters={"type": "object", "properties": {"prefix": {"type": "string", "description": "optional sub-folder"}}}),
    ToolSpec(name="read_file", description="Read a text file.", mutating=False,
             parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ToolSpec(name="write_file", description="Create or overwrite a text file with the exact content given.", mutating=True,
             parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                         "required": ["path", "content"]}),
    ToolSpec(name="delete_file", description="Delete a file.", mutating=True,
             parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
]


def _skipped(rel_parts: tuple[str, ...], excluded: set[str]) -> bool:
    return any(p in SKIP_DIRS or p.startswith(".") for p in rel_parts[:-1]) or (rel_parts and rel_parts[0] in excluded)


def _iter_files(root: Path, excluded: set[str]):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _skipped(rel.parts, excluded):
            continue
        yield p, rel.as_posix()


def _describe(path: Path, rel: str) -> dict[str, Any]:
    data = path.read_bytes()
    rec: dict[str, Any] = {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content": None}
    if len(data) <= MAX_INLINE:
        try:
            rec["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            rec["content"] = None
    return rec


def folder_schema(source: SourceConfig) -> EnvironmentSchema:
    return EnvironmentSchema(
        name=f"src_{source.id}",
        title=f"{source.name} (folder)",
        description=f"Connected folder {source.location}",
        entities=[EntitySpec(
            name="file", collection="files", display_name="File", aliases=["files", "document", "text file", "doc"],
            key_field="path", key_type="path", label_field="path",
            fields=[FieldSpec(name="path", description="Relative path inside the folder"),
                    FieldSpec(name="content", type="text", description="Exact UTF-8 text content",
                              synonyms=["content", "contents", "text", "body"]),
                    FieldSpec(name="size", type="integer"), FieldSpec(name="sha256")],
            managed_fields=["size", "sha256"])],
        tools=FOLDER_TOOLS,
        exclusive_access=source.exclusive,
    )


class FolderToolbox(Toolbox):
    def __init__(self, root: Path, excluded: set[str]) -> None:
        self._root = root
        self._excluded = excluded

    @property
    def specs(self) -> list[ToolSpec]:
        return FOLDER_TOOLS

    def _rel(self, value: Any) -> tuple[Path, str]:
        rel = normalize_relative_path(value)
        parts = tuple(rel.split("/"))
        if _skipped(parts, self._excluded):
            raise UnsafePathError(f"'{rel}' is inside an excluded or hidden folder")
        return safe_join(self._root, rel), rel

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        if inject_failure:
            return ToolResult.failure("PermissionError: the file is being used by another process")
        args = args if isinstance(args, dict) else {}
        try:
            if name == "list_files":
                prefix = str(args.get("prefix") or "").strip().strip("/")
                files = [{"path": rel, "size": p.stat().st_size} for p, rel in _iter_files(self._root, self._excluded)
                         if not prefix or rel.startswith(prefix + "/")]
                return ToolResult.success({"files": files[:500], "total": len(files)})
            target, rel = self._rel(args.get("path"))
            if name == "read_file":
                if not target.is_file():
                    return ToolResult.failure(f"File not found: {rel}", status_code=404)
                rec = _describe(target, rel)
                if rec["content"] is None:
                    return ToolResult.failure(f"{rel} is not a readable UTF-8 text file (or larger than 256 KiB).", status_code=415)
                return ToolResult.success(rec)
            if name == "write_file":
                content = args.get("content")
                if not isinstance(content, str):
                    return ToolResult.failure("content must be a string", status_code=400)
                data = content.encode("utf-8")
                if len(data) > MAX_WRITE:
                    return ToolResult.failure("content exceeds the 2 MiB limit", status_code=413)
                existed = target.exists()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                return ToolResult.success({"path": rel, "bytes_written": len(data), "overwritten": existed})
            if name == "delete_file":
                if not target.is_file():
                    return ToolResult.failure(f"File not found: {rel}", status_code=404)
                target.unlink()
                return ToolResult.success({"path": rel, "deleted": True})
            return ToolResult.failure(f"Unknown tool {name!r}")
        except UnsafePathError as exc:
            return ToolResult.failure(f"Unsafe path: {exc}", status_code=400)
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}", status_code=500)


class FolderInspector(StateInspector):
    source = "verifier:filesystem-readonly"

    def __init__(self, root: Path, excluded: set[str]) -> None:
        self._root = root
        self._excluded = excluded

    def snapshot(self) -> Snapshot:
        if not self._root.is_dir():
            raise InspectorUnavailable(f"Folder not found: {self._root}")
        try:
            files: dict[str, dict[str, Any]] = {}
            total = 0
            for p, rel in _iter_files(self._root, self._excluded):
                total += p.stat().st_size
                if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise InspectorUnavailable(
                        f"The folder exceeds the inspection limit ({MAX_FILES} files / {MAX_TOTAL_BYTES // (1024 * 1024)} MiB). "
                        "Connect a smaller sub-folder.")
                files[rel] = _describe(p, rel)
            return {"files": files}
        except OSError as exc:
            raise InspectorUnavailable(f"Folder inspection failed: {exc}") from exc


class FolderSourceEnvironment(Environment):
    def __init__(self, config: SourceConfig) -> None:
        super().__init__()
        self.config = config
        self.root = Path(config.location)
        self.name = f"src_{config.id}"
        self.resource_identifier = str(self.root)
        self.warnings: list[str] = []
        self._excluded = {e.strip().strip("/") for e in config.excluded if e.strip()}
        self._schema = folder_schema(config)
        self.ensure_initialized()

    @property
    def schema(self) -> EnvironmentSchema:
        return self._schema

    def refresh(self) -> None:
        self.ensure_initialized()

    def ensure_initialized(self) -> None:
        if not self.root.is_dir():
            raise FileNotFoundError(f"Folder not found: {self.root}")

    def reset(self) -> None:
        raise NotImplementedError("Connected data sources are never reset; use 'Undo run' to restore a backup.")

    def toolbox(self) -> FolderToolbox:
        return FolderToolbox(self.root, self._excluded)

    def inspector(self) -> FolderInspector:
        return FolderInspector(self.root, self._excluded)

    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        if tool_name in ("write_file", "delete_file", "read_file") and isinstance(args, dict) and args.get("path"):
            try:
                return [("files", normalize_relative_path(args["path"]))]
            except UnsafePathError:
                return []
        return []

    def backup(self, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for p, rel in _iter_files(self.root, self._excluded):
                z.write(p, rel)
        return dest.stat().st_size

    def restore(self, backup_file: Path) -> None:
        """Make the folder's (non-excluded) files exactly match the backup."""
        if not backup_file.is_file():
            raise FileNotFoundError(f"Backup not found: {backup_file}")
        with zipfile.ZipFile(backup_file) as z:
            names = set(z.namelist())
            for p, rel in list(_iter_files(self.root, self._excluded)):
                if rel not in names:
                    p.unlink()
            for name in names:
                target = safe_join(self.root, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
