from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from hearth.runtime import PendingConfirm, runtime
from hearth import widgets as widget_bus

if TYPE_CHECKING:  # pragma: no cover — avoids a jev <-> registry import cycle
    from hearth.jev.tools import ToolDecision

log = logging.getLogger("hearth.tools")

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ConfiguredFn = Callable[[], bool]
PreviewFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    # When True, the registry dry-runs until confirm=true (UI/voice Confirm).
    # Reserve for high-risk / irreversible / paid actions only — routine house
    # tools (lights, play, grabs, CoS escalate, sandbox writes) auto-run.
    destructive: bool = False
    source: str = "builtin"
    configured: ConfiguredFn | None = None
    not_configured: str = ""
    # Optional enricher for dry-run / confirm previews (e.g. cart + address).
    preview: PreviewFn | None = None
    # Optional: async resolve during destructive dry-run (e.g. plex_play plan).
    # Return ok=False to surface ambiguity/errors without a confirm button.
    preview_handler: Handler | None = None


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: dict[str, Any]
    needs_confirm: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        from hearth.runtime import _now

        return {
            "name": self.name,
            "ok": self.ok,
            "needs_confirm": self.needs_confirm,
            "dry_run": self.dry_run,
            "data": self.data,
            "ts": _now(),
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

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        said: str = "",
        explicit_confirm: bool = False,
        gate: bool = True,
    ) -> ToolResult:
        """Run one tool after Jev has decided it may run.

        ``said`` is the user text the gate reasons about (defaults to the open
        turn's text). ``explicit_confirm`` marks a call the user already
        confirmed — a button tap or a typed yes — so only Jev's hard stops apply.
        ``gate=False`` is for internal replays that were already authorized.
        """
        args = dict(args or {})
        spec = self._tools.get(name)
        runtime.begin_tool(name)
        if spec is None:
            result = ToolResult(name=name, ok=False, data={"error": f"unknown tool {name}"})
            return _finish_tool(result)

        if spec.configured is not None and not spec.configured():
            message = spec.not_configured or f"{name} is not configured"
            result = ToolResult(
                name=name,
                ok=False,
                data={"ok": False, "configured": False, "error": message},
            )
            return _finish_tool(result)

        decision: ToolDecision | None = None
        if gate:
            decision = await _jev_decision(
                name,
                args,
                said=said,
                explicit_confirm=explicit_confirm,
            )
        if decision is not None and decision.denied:
            result = ToolResult(
                name=name,
                ok=False,
                data={
                    "ok": False,
                    "denied": True,
                    "error": f"jev denied {name}: {decision.reason}",
                    "speak": decision.message,
                    "jev": decision.as_log_dict(),
                },
            )
            # A governance decision is not a backend failure; don't flash an error.
            return _finish_tool(result, flash_error=False)

        # Jev may ask for a confirm on a tool that is not marked destructive.
        confirm_gated = spec.destructive or (decision is not None and decision.needs_confirm)
        if confirm_gated:
            confirm = bool(args.get("confirm"))
            dry_run = args.get("dry_run")
            if dry_run is None:
                dry_run = not confirm
            if not confirm:
                preview_args = {k: v for k, v in args.items() if k not in {"confirm", "dry_run"}}
                plan: dict[str, Any] | None = None
                if spec.preview_handler is not None:
                    try:
                        planned = await spec.preview_handler(preview_args)
                    except Exception as exc:  # noqa: BLE001
                        result = ToolResult(name=name, ok=False, data={"error": str(exc)})
                        return _finish_tool(result)
                    plan = planned if isinstance(planned, dict) else {"result": planned}
                    if plan.get("ok") is False:
                        if plan.get("needs_client") and plan.get("retryable"):
                            # Keep the play intent so UI/voice can retry after Plex opens.
                            runtime.pending = PendingConfirm(
                                tool=name,
                                args=preview_args,
                                preview=f"{name} awaiting client: {preview_args}",
                                reason="awaiting_client",
                            )
                            result = ToolResult(
                                name=name,
                                ok=False,
                                needs_confirm=True,
                                dry_run=True,
                                data={
                                    **plan,
                                    "tool": name,
                                    "would_call_with": preview_args,
                                    "hint": (
                                        "Open Plex on the target client, then confirm / "
                                        "Try again — Hearth will re-detect and start."
                                    ),
                                },
                            )
                            # Waiting on the user — not a hard backend failure.
                            return _finish_tool(result, flash_error=False)
                        runtime.pending = None
                        result = ToolResult(name=name, ok=False, data=plan)
                        finished = _finish_tool(result)
                        _offer_memory(spec, finished)
                        return finished
                preview: dict[str, Any] = {
                    "tool": name,
                    "would_call_with": preview_args,
                    "hint": "Re-run with confirm=true to execute. High-risk tools default to dry-run.",
                }
                if decision is not None and decision.needs_confirm:
                    preview["jev"] = decision.as_log_dict()
                    preview["speak"] = (
                        f"That reads risky enough to check first. Confirm and I'll run {name}."
                    )
                if plan is not None:
                    preview["plan"] = plan
                    if plan.get("speak"):
                        preview["speak"] = plan["speak"]
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
                    reason="confirm",
                )
                result = ToolResult(
                    name=name,
                    ok=True,
                    needs_confirm=True,
                    dry_run=True,
                    data=preview,
                )
                finished = _finish_tool(result, flash_error=False)
                _offer_memory(spec, finished)
                return finished
            args["confirm"] = True
            args["dry_run"] = False

        try:
            data = await spec.handler(args)
        except Exception as exc:  # noqa: BLE001 — surface tool errors to the agent
            result = ToolResult(name=name, ok=False, data={"error": str(exc)})
            return _finish_tool(result)

        payload_data = data if isinstance(data, dict) else {"result": data}
        ok = not (isinstance(payload_data, dict) and payload_data.get("ok") is False)

        if (
            isinstance(payload_data, dict)
            and payload_data.get("needs_client")
            and payload_data.get("retryable")
        ):
            # Play waiting for a client — keep retry pending (works for lenient plex_play too).
            keep_args = {k: v for k, v in args.items() if k not in {"confirm", "dry_run"}}
            runtime.pending = PendingConfirm(
                tool=name,
                args=keep_args,
                preview=f"{name} awaiting client: {keep_args}",
                reason="awaiting_client",
            )
            result = ToolResult(
                name=name,
                ok=False,
                needs_confirm=True,
                dry_run=not bool(args.get("confirm")),
                data=payload_data,
            )
            finished = _finish_tool(result, flash_error=False)
            _offer_memory(spec, finished)
            return finished

        if confirm_gated or (runtime.pending is not None and runtime.pending.tool == name):
            runtime.pending = None
        result = ToolResult(name=name, ok=ok, data=payload_data)
        finished = _finish_tool(result)
        _offer_memory(spec, finished)
        return finished


async def _jev_decision(
    name: str,
    args: dict[str, Any],
    *,
    said: str,
    explicit_confirm: bool,
) -> ToolDecision | None:
    """Ask the Jev tool gate about this call. ``None`` means "no opinion, run it"."""
    from hearth.config import settings

    if not (settings.jev_enabled and settings.jev_tool_gate):
        return None
    try:
        from hearth.jev.tools import authorize_tool

        return await authorize_tool(
            name,
            args,
            said=said or None,
            explicit_confirm=explicit_confirm,
        )
    except Exception:  # noqa: BLE001 — the gate must never break a tool call
        log.warning("jev tool gate failed open for %s", name, exc_info=True)
        return None


def _finish_tool(result: ToolResult, *, flash_error: bool = True) -> ToolResult:
    """Record the tool result, publish widgets, and update the UI activity."""
    payload = result.as_dict()
    runtime.last_tools.append(payload)
    widget_bus.publish_tool(payload)
    if not result.ok and flash_error:
        data = result.data if isinstance(result.data, dict) else {}
        # Unconfigured integrations are soft misses, not scary UI errors.
        if data.get("configured") is not False:
            err = str(data.get("error") or data.get("message") or "")
            runtime.end_tool(ok=False, error=err, tool=result.name)
    return result


def _offer_memory(spec: ToolSpec, result: ToolResult) -> None:
    try:
        from hearth.memory.events import on_tool_result

        on_tool_result(spec, result)
    except Exception:  # noqa: BLE001 — memory must not break tools
        return


registry = ToolRegistry()
