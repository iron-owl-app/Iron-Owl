"""Start FinTrack on the loopback interface: `python run.py` (from backend/, venv active)."""
from __future__ import annotations

import logging
import sys

import uvicorn

from app.config import get_settings
from app.main import create_app

LOOPBACK_ALIASES = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}


def main() -> None:
    settings = get_settings()
    host = LOOPBACK_ALIASES.get(settings.host.strip().lower())
    if host is None:
        # Never expose the vault on a network interface.
        sys.exit(f"Refusing to bind to non-loopback HOST={settings.host!r}; use 127.0.0.1.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.frontend_dist.joinpath("index.html").is_file():
        logging.getLogger("fintrack").info("serving frontend from %s", settings.frontend_dist)

    uvicorn.run(
        create_app(settings),
        host=host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        log_level="info",
        # A request still open at shutdown (e.g. the "Change folder…" window) can't hold up
        # the exit forever; the shutdown lock then closes that window.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
