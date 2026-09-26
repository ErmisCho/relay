"""``uv run python -m relay.delegator``: serve the Delegator with uvicorn."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "relay.delegator.app:create_app",
        factory=True,
        host=os.environ.get("DELEGATOR_HOST", "127.0.0.1"),
        port=int(os.environ.get("DELEGATOR_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
