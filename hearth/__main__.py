"""Run the Hearth runtime: ``python -m hearth``."""

from __future__ import annotations

import uvicorn

from hearth.config import settings


def main() -> None:
    uvicorn.run(
        "hearth.app:app",
        host=settings.bind_host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
