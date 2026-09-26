"""`uv run python -m relay.executor` — run the executor worker."""

from __future__ import annotations

import os

import uvicorn

from relay.executor.worker import create_app


def main() -> None:
    host = os.environ.get("EXECUTOR_HOST", "127.0.0.1")
    port = int(os.environ.get("EXECUTOR_PORT", "8001"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
