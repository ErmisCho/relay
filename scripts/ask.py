"""Ask the Delegator one question end to end, then shut it down.

Usage:  uv run python scripts/ask.py ["your question"]

Starts Postgres (docker compose), applies migrations, runs the Delegator on a
free local port with your .env, sends the question, prints the reply, stops the
Delegator. Postgres is left running (docker compose down to stop it).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from relay.config import Settings

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTION = "What are my PC specs? Oh, by the way, what is the weather in Berlin?"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_delegator() -> tuple[subprocess.Popen[bytes], int, Path]:
    """Postgres up, migrations applied, Delegator running on a free port.

    Returns (process, port, log path); exits with a message if it never comes up.
    """
    print("Starting Postgres and applying migrations...", flush=True)
    subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    port = free_port()
    log = Path(tempfile.gettempdir()) / "relay-delegator.log"
    env = {**os.environ, "DELEGATOR_PORT": str(port)}
    with log.open("w") as out:
        server = subprocess.Popen(
            [sys.executable, "-m", "relay.delegator"],
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    print("Starting the Delegator...", flush=True)
    deadline = time.monotonic() + 60
    while "Uvicorn running" not in log.read_text(errors="replace"):
        if server.poll() is not None or time.monotonic() > deadline:
            server.kill()
            raise SystemExit(f"Delegator failed to start; see {log}")
        time.sleep(0.5)
    return server, port, log


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # "°C" on a Windows console
    question = " ".join(sys.argv[1:]) or DEFAULT_QUESTION
    server, port, _ = start_delegator()
    try:
        print(f"\nYou: {question}\n", flush=True)
        resp = httpx.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": f"Bearer {Settings().delegator_shared_secret}"},
            json={
                "model": "relay",
                "stream": False,
                "messages": [{"role": "user", "content": question}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        print("Relay:", resp.json()["choices"][0]["message"]["content"])
        return 0
    finally:
        server.kill()
        server.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
