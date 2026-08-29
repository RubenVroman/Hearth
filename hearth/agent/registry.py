from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hearth.runtime import PendingConfirm, runtime
from hearth import widgets as widget_bus

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ConfiguredFn = Callable[[], bool]
PreviewFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    destructive: bool = False
    source: str = "builtin"
    configured: ConfiguredFn | None = None
    not_configured: str = ""
    # Optional enricher for dry-run / confirm previews (e.g. cart + address).
    preview: PreviewFn | None = None


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: dict[str, Any]
    needs_confirm: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "needs_confirm": self.needs_confirm,
            "dry_run": self.dry_run,
            "data": self.data,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_public(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "destructive": t.destructive,
                "source": t.source,
                "parameters": t.parameters,
            }
            for t in sorted(self._tools.values(), key=lambda x: x.name)
        ]

    def openai_chat_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def openai_realtime_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        args = dict(args or {})
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name=name, ok=False, data={"error": f"unknown tool {name}"})

        if spec.configured is not None and not spec.configured():
            message = spec.not_configured or f"{name} is not configured"
            result = ToolResult(
                name=name,
                ok=False,
                data={"ok": False, "configured": False, "error": message},
            )
            payload = result.as_dict()
            runtime.last_tools.append(payload)
            widget_bus.publish_tool(payload)
            return result

        if spec.destructive:
            confirm = bool(args.get("confirm"))
            dry_run = args.get("dry_run")
            if dry_run is None:
                dry_run = not confirm
            if not confirm:
                preview_args = {k: v for k, v in args.items() if k not in {"confirm", "dry_run"}}
                preview = {
                    "tool": name,
                    "would_call_with": preview_args,
                    "hint": "Re-run with confirm=true to execute. Destructive tools default to dry-run.",
                }
                if spec.preview is not None:
                    try:
                        extra = spec.preview(preview_args)
                        if isinstance(extra, dict):
                            preview.update(extra)
                    except Exception as exc:  # noqa: BLE001
                        preview["preview_error"] = str(exc)
                runtime.pending = PendingConfirm(
                    tool=name,
                    args=args,
                    preview=f"{name} {preview_args}",
                )
                result = ToolResult(
                    name=name,
                    ok=True,
                    needs_confirm=True,
                    dry_run=True,
                    data=preview,
                )
                payload = result.as_dict()
                runtime.last_tools.append(payload)
                widget_bus.publish_tool(payload)
                _offer_memory(spec, result)
                return result
            args["confirm"] = True
            args["dry_run"] = False

        try:
            data = await spec.handler(args)
        except Exception as exc:  # noqa: BLE001 — surface tool errors to the agent
            result = ToolResult(name=name, ok=False, data={"error": str(exc)})
            payload = result.as_dict()
            runtime.last_tools.append(payload)
            widget_bus.publish_tool(payload)
            return result

        if spec.destructive:
            runtime.pending = None
        payload_data = data if isinstance(data, dict) else {"result": data}
        ok = not (isinstance(payload_data, dict) and payload_data.get("ok") is False)
        result = ToolResult(name=name, ok=ok, data=payload_data)
        payload = result.as_dict()
        runtime.last_tools.append(payload)
        widget_bus.publish_tool(payload)
        _offer_memory(spec, result)
        return result


def _offer_memory(spec: ToolSpec, result: ToolResult) -> None:
    try:
        from hearth.memory.events import on_tool_result

        on_tool_result(spec, result)
    except Exception:  # noqa: BLE001 — memory must not break tools
        return


registry = ToolRegistry()
