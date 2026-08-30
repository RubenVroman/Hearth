from __future__ import annotations

from pathlib import Path
from typing import Any

from hearth.config import settings

MAX_READ_BYTES = 64_000


def workspace_root() -> Path:
    root = settings.workspace_path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return root


def safe_path(rel: str) -> Path:
    if not rel or rel.strip() != rel:
        raise ValueError("path required")
    raw = rel.strip()
    if raw.startswith("/") or raw.startswith("~") or ".." in Path(raw).parts:
        raise ValueError("path escapes workspace")
    root = workspace_root()
    target = (root / raw).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path escapes workspace")
    return target


def list_dir(rel: str = ".") -> dict[str, Any]:
    target = safe_path(rel) if rel not in {".", "", "./"} else workspace_root()
    if not target.exists():
        return {"ok": False, "error": "not found"}
    if not target.is_dir():
        return {"ok": False, "error": "not a directory"}
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child.relative_to(workspace_root())),
                "type": "dir" if child.is_dir() else "file",
                "bytes": child.stat().st_size if child.is_file() else None,
            }
        )
    return {"ok": True, "root": str(workspace_root()), "path": rel or ".", "entries": entries}


def read_file(rel: str) -> dict[str, Any]:
    target = safe_path(rel)
    if not target.is_file():
        return {"ok": False, "error": "not a file"}
    data = target.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    return {
        "ok": True,
        "path": rel,
        "truncated": truncated,
        "text": text,
    }


def write_file(rel: str, content: str) -> dict[str, Any]:
    target = safe_path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.is_relative_to(workspace_root()):
        raise ValueError("path escapes workspace")
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": rel, "bytes": target.stat().st_size}


def delete_file(rel: str) -> dict[str, Any]:
    target = safe_path(rel)
    if not target.exists():
        return {"ok": False, "error": "not found"}
    if target.is_dir():
        if any(target.iterdir()):
            return {"ok": False, "error": "directory not empty"}
        target.rmdir()
    else:
        target.unlink()
    return {"ok": True, "deleted": rel}
