"""File-system sandbox rooted at ``data/sandbox/files``.

Every path is normalised and confined to the sandbox root (absolute paths,
drive letters, ``..`` traversal and reserved device names are rejected).
Snapshots record size and SHA-256 for every file, plus the text content of
small UTF-8 files so reports can show what actually changed.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

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

MAX_INLINE_CONTENT = 64 * 1024
MAX_WRITE_BYTES = 1024 * 1024

SCHEMA = EnvironmentSchema(
    name="file",
    title="File workspace (local sandbox folder)",
    description="A sandboxed folder of synthetic text files (reports, notes, config, logs).",
    entities=[
        EntitySpec(
            name="file",
            collection="files",
            display_name="File",
            aliases=["files", "document", "text file", "doc"],
            key_field="path",
            key_type="path",
            label_field="path",
            fields=[
                FieldSpec(name="path", type="string", description="Relative path inside the sandbox, e.g. reports/summary.txt"),
                FieldSpec(name="content", type="text", description="Exact UTF-8 text content of the file",
                          synonyms=["content", "contents", "text", "body"]),
                FieldSpec(name="size", type="integer", description="Size in bytes (derived)"),
                FieldSpec(name="sha256", type="string", description="SHA-256 checksum (derived)"),
            ],
            managed_fields=["size", "sha256"],
        )
    ],
    tools=[
        ToolSpec(name="file_list", description="List files in the sandbox", mutating=False),
        ToolSpec(name="file_read", description="Read a file", mutating=False),
        ToolSpec(name="file_write", description="Create or overwrite a file", mutating=True),
        ToolSpec(name="file_delete", description="Delete a file", mutating=True),
    ],
)


def _describe(path: Path, rel: str) -> dict[str, Any]:
    data = path.read_bytes()
    record: dict[str, Any] = {
        "path": rel,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": None,
    }
    if len(data) <= MAX_INLINE_CONTENT:
        try:
            record["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            record["content"] = None
    return record


class FileToolbox(Toolbox):
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def specs(self) -> list[ToolSpec]:
        return SCHEMA.tools

    def call(self, name: str, args: dict[str, Any], *, inject_failure: bool = False) -> ToolResult:
        if inject_failure:
            return ToolResult.failure(
                "PermissionError: [WinError 32] The process cannot access the file because it is being used by another process"
            )
        try:
            if name == "file_list":
                files = sorted(p.relative_to(self._root).as_posix() for p in self._root.rglob("*") if p.is_file())
                return ToolResult.success({"files": files})
            path_arg = args.get("path")
            target = safe_join(self._root, path_arg)
            rel = normalize_relative_path(path_arg)
            if name == "file_read":
                if not target.is_file():
                    return ToolResult.failure(f"FileNotFoundError: {rel}")
                return ToolResult.success(_describe(target, rel))
            if name == "file_write":
                content = args.get("content")
                if not isinstance(content, str):
                    return ToolResult.failure("content must be a string")
                data = content.encode("utf-8")
                if len(data) > MAX_WRITE_BYTES:
                    return ToolResult.failure("content exceeds 1 MiB sandbox limit")
                existed = target.exists()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)  # bytes: no newline translation on Windows
                return ToolResult.success({"path": rel, "bytes_written": len(data), "overwritten": existed})
            if name == "file_delete":
                if not target.is_file():
                    return ToolResult.failure(f"FileNotFoundError: {rel}")
                target.unlink()
                return ToolResult.success({"path": rel, "deleted": True})
            return ToolResult.failure(f"Unknown tool {name!r}")
        except UnsafePathError as exc:
            return ToolResult.failure(f"UnsafePathError: {exc}")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class FileInspector(StateInspector):
    source = "verifier:filesystem-readonly"

    def __init__(self, root: Path) -> None:
        self._root = root

    def snapshot(self) -> Snapshot:
        if not self._root.is_dir():
            raise InspectorUnavailable("File sandbox directory is missing")
        try:
            files: dict[str, dict[str, Any]] = {}
            for p in sorted(self._root.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(self._root).as_posix()
                    files[rel] = _describe(p, rel)
            return {"files": files}
        except OSError as exc:
            raise InspectorUnavailable(f"File inspection failed: {exc}") from exc


class FileEnvironment(Environment):
    name = "file"

    def __init__(self, sandbox_dir: Path, sample_data_dir: Path) -> None:
        super().__init__()
        self.root = sandbox_dir / "files"
        self.seed_dir = sample_data_dir / "sandbox_files"
        self.resource_identifier = "sandbox/files/"

    @property
    def schema(self) -> EnvironmentSchema:
        return SCHEMA

    def ensure_initialized(self) -> None:
        if not self.root.is_dir():
            self.reset()

    def reset(self) -> None:
        with self.lock:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            for src in sorted(self.seed_dir.rglob("*")):
                if src.is_file():
                    dest = self.root / src.relative_to(self.seed_dir)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Normalise seed files to LF so checksums are platform independent.
                    dest.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))

    def toolbox(self) -> FileToolbox:
        return FileToolbox(self.root)

    def inspector(self) -> FileInspector:
        return FileInspector(self.root)

    def trace_refs(self, tool_name: str | None, args: Any, output: Any) -> list[tuple[str, str]]:
        if tool_name in ("file_write", "file_delete", "file_read") and isinstance(args, dict) and args.get("path"):
            try:
                return [("files", normalize_relative_path(args["path"]))]
            except UnsafePathError:
                return []
        return []
