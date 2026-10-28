"""HTTP surface for the living-room shell. HTML is public; actions are not."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hearth.tv.board import TvActionError, build_board, run_action

TV_INDEX = Path(__file__).resolve().parents[1] / "ui" / "static" / "tv" / "index.html"

tv_router = APIRouter(tags=["tv"])


class TvActionBody(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    id: str = Field(default="", max_length=64)
    title: str = Field(default="", max_length=200)
    season: int | None = Field(default=None, ge=0, le=99)
    episode: int | None = Field(default=None, ge=0, le=999)


@tv_router.get("/tv")
async def tv_page() -> FileResponse:
    """10-foot command center. Public so the TV can show its own login."""
    if not TV_INDEX.is_file():
        raise HTTPException(status_code=404, detail="tv shell missing")
    return FileResponse(TV_INDEX, headers={"Cache-Control": "no-cache"})


@tv_router.get("/api/tv/board")
async def tv_board() -> dict:
    return await build_board()


@tv_router.post("/api/tv/action")
async def tv_action(body: TvActionBody) -> dict:
    try:
        return await run_action(
            kind=body.kind,
            action_id=body.id,
            title=body.title,
            season=body.season,
            episode=body.episode,
        )
    except TvActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
