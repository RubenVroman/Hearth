"""Load Python skills from the sandboxed workspace."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from hearth.agent.registry import ToolSpec, registry
from hearth.tools import files as workspace_files

SKILL_PREFIX = "hearth_skill_"


def skills_dir() -> Path:
    path = workspace_files.workspace_root() / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_workspace_skills() -> list[str]:
    loaded: list[str] = []
    for path in sorted(skills_dir().glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec_name = _import_skill(path)
        except Exception:  # noqa: BLE001
            continue
        if spec_name:
            loaded.append(spec_name)
    return loaded


def _import_skill(path: Path) -> str | None:
    mod_name = SKILL_PREFIX + path.stem
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    name = getattr(module, "NAME", path.stem)
    description = getattr(module, "DESCRIPTION", f"Workspace skill {name}")
    parameters = getattr(module, "PARAMETERS", {"type": "object", "properties": {}})
    destructive = bool(getattr(module, "DESTRUCTIVE", False))
    run = getattr(module, "run", None)
    if not callable(run):
        return None

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = run(args, {"workspace": str(workspace_files.workspace_root())})
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[misc]
        return result if isinstance(result, dict) else {"result": result}

    registry.register(
        ToolSpec(
            name=str(name),
            description=str(description),
            parameters=parameters if isinstance(parameters, dict) else {"type": "object"},
            handler=handler,
            destructive=destructive,
            source=f"workspace:{path.name}",
        )
    )
    return str(name)
