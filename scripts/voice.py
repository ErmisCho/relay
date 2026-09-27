"""Talk to relay by voice: one command, Ctrl-C to stop.

Usage:  uv run python scripts/voice.py

Starts Postgres + the Delegator (same as scripts/ask.py), opens your ngrok
tunnel on DELEGATOR_PUBLIC_URL so ElevenLabs can reach it, and opens a
local page (127.0.0.1) to talk to the agent with the Delegator log alongside.
Needs ngrok installed and logged in once: ngrok config add-authtoken <token>.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
from ask import start_delegator

from relay.config import Settings

PAGE = Path(__file__).with_name("voice_ui.html")


def serve_ui(settings: Settings, log: Path) -> ThreadingHTTPServer:
    """The local voice page on 127.0.0.1 only - never through the tunnel.

    /signed-url spends ElevenLabs minutes, so it must not be reachable from outside.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
            elif self.path == "/signed-url":
                # A one-time URL keeps the agent's auth on (no public talk-to link needed).
                r = httpx.get(
                    "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                    params={"agent_id": settings.elevenlabs_agent_id},
                    headers={"xi-api-key": settings.elevenlabs_api_key or ""},
                    timeout=15,
                )
                self._send(r.status_code, "application/json", r.content)
            elif self.path == "/logs":
                self._stream_log()
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, kind: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream_log(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = 0
            try:
                while True:
                    # ponytail: re-reads the whole log each second; fine for one dev session.
                    lines = log.read_text(errors="replace").splitlines()
                    for line in lines[sent:]:
                        self.wfile.write(f"data: {line}\n\n".encode())
                    sent = len(lines)
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        def log_message(self, format: str, *args: object) -> None:
            pass

    ui = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ui.daemon_threads = True
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    return ui


def main() -> int:
    settings = Settings()
    domain = urlparse(settings.delegator_public_url).hostname
    ngrok = shutil.which("ngrok")
    if not ngrok:
        print("ngrok not found. Install it from https://ngrok.com/download, then run:")
        print("  ngrok config add-authtoken <your token from dashboard.ngrok.com>")
        return 1
    if not domain or domain in ("localhost", "127.0.0.1") or not settings.elevenlabs_agent_id:
        print("Set DELEGATOR_PUBLIC_URL (your ngrok domain) and ELEVENLABS_AGENT_ID in .env.")
        return 1

    try:  # read-only: a mismatch means ElevenLabs calls some other URL and nothing arrives
        agent = httpx.get(
            f"https://api.elevenlabs.io/v1/convai/agents/{settings.elevenlabs_agent_id}",
            headers={"xi-api-key": settings.elevenlabs_api_key or ""},
            timeout=15,
        ).json()
        agent_url = agent["conversation_config"]["agent"]["prompt"]["custom_llm"]["url"]
        if urlparse(agent_url).hostname != domain:
            print(f"WARNING: the ElevenLabs agent calls {agent_url}, not https://{domain}.")
            print("  Set its Custom LLM URL to", f"https://{domain}/v1", "or no turns arrive.\n")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        print("(Could not read the ElevenLabs agent config to check its URL.)\n")

    server, port, log = start_delegator()
    tunnel_log = Path(tempfile.gettempdir()) / "relay-ngrok.log"
    with tunnel_log.open("w") as out:
        tunnel = subprocess.Popen(
            [ngrok, "http", str(port), f"--domain={domain}", "--log=stdout"],
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        print(f"Opening the tunnel https://{domain} ...", flush=True)
        deadline = time.monotonic() + 30
        while "started tunnel" not in tunnel_log.read_text(errors="replace"):
            if tunnel.poll() is not None or time.monotonic() > deadline:
                print("ngrok failed to start:\n" + tunnel_log.read_text(errors="replace")[-1500:])
                return 1
            time.sleep(0.5)

        ui = serve_ui(settings, log)
        page = f"http://127.0.0.1:{ui.server_port}"
        print(f"\nReady. Opening {page} - click Start call. Ctrl-C here to stop.\n", flush=True)
        webbrowser.open(page)
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(1)
        print("The Delegator or ngrok stopped; see", log, "and", tunnel_log)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        tunnel.kill()
        server.kill()
        print("\nStopped the Delegator and the tunnel (Postgres left running).")


if __name__ == "__main__":
    sys.exit(main())
