"""``uv run python -m relay.delegator``: serve the Delegator with uvicorn."""

from __future__ import annotations

import logging
import os
import time
from types import FrameType

import uvicorn

log = logging.getLogger("relay.delegator")

#: Repeated SIGINTs within this window after the first are ignored so the graceful shutdown
#: (``DelegatorService.drain``, itself bounded) can finish. Under ``uv run`` one Ctrl-C arrives
#: twice: once via the terminal's process group and once forwarded by uv.
REPEAT_SIGINT_GRACE_S = 15.0


class GracefulServer(uvicorn.Server):
    """First SIGINT/SIGTERM starts a graceful shutdown; repeats only force it after a grace."""

    _first_exit_at: float | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        now = time.monotonic()
        if self._first_exit_at is None:
            self._first_exit_at = now
            log.info("delegator: signal %d received, shutting down gracefully", sig)
            super().handle_exit(sig, frame)
            return
        if now - self._first_exit_at < REPEAT_SIGINT_GRACE_S:
            log.info("delegator: repeated signal %d ignored while shutting down", sig)
            return
        log.warning(
            "delegator: repeated signal %d after %.0fs, forcing exit",
            sig,
            now - self._first_exit_at,
        )
        self.force_exit = True
        self._captured_signals.append(sig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = uvicorn.Config(
        "relay.delegator.app:create_app",
        factory=True,
        host=os.environ.get("DELEGATOR_HOST", "127.0.0.1"),
        port=int(os.environ.get("DELEGATOR_PORT", "8000")),
    )
    GracefulServer(config).run()


if __name__ == "__main__":
    main()
