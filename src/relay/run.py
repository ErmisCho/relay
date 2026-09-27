"""``uv run relay``: start the whole backend with one command; Ctrl-C stops all of it.

Steps: check the ports are free, start Postgres (docker compose) and wait until it is healthy,
apply the schema migrations, rebuild the demo website if its sources changed, then run the
executor, the Delegator (demo API on) and, when ``DELEGATOR_PUBLIC_URL`` is set, the ngrok
tunnel ElevenLabs reaches the Delegator through, as child processes with prefixed logs.
Ctrl-C or SIGTERM stops all children gracefully; if any child dies, the rest are stopped too.
Postgres keeps running (``docker compose stop postgres`` stops it). Models and keys come
from ``.env``.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import FrameType

#: A child still running this long after SIGTERM is killed. The Delegator drains in-flight
#: turns on SIGTERM (``DelegatorService.drain``), which is bounded well below this.
STOP_TIMEOUT_S = 20.0
#: How long to wait for the public URL to answer through the tunnel before warning.
TUNNEL_WAIT_S = 30.0


def repo_root() -> Path:
    """The checkout this module runs from (an editable install, as ``uv run`` makes it)."""
    root = Path(__file__).resolve().parents[2]
    if not (root / "docker-compose.yml").is_file():
        sys.exit(f"relay: expected docker-compose.yml in {root}; run from a relay checkout")
    return root


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def port_owner(port: int) -> str | None:
    """None if 127.0.0.1:``port`` is free, else a description of who holds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Like uvicorn: without SO_REUSEADDR a connection left in TIME_WAIT by the previous
        # run makes the port look taken for a minute although nothing listens on it. Not on
        # Windows: there SO_REUSEADDR binds even over a live listener, hiding the conflict.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return None
        except OSError:
            pass
    if shutil.which("lsof") is None:  # Windows: no lsof/ps to name the owner
        return "another process"
    found = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True
    ).stdout.split()
    if not found:
        return "an unknown process"
    command = subprocess.run(
        ["ps", "-o", "command=", "-p", found[0]], capture_output=True, text=True
    ).stdout.strip()
    return f"pid {found[0]} ({command})"


def docker_command() -> str:
    """``docker``, also where Docker Desktop installs it without putting it on PATH."""
    found = shutil.which("docker") or shutil.which("docker", path=str(Path.home() / ".docker/bin"))
    if found is None:
        sys.exit("relay: docker not found; install or start Docker Desktop")
    return found


def run_step(argv: list[str], cwd: Path) -> None:
    """Run a setup command in the foreground; exit with its message if it fails."""
    if subprocess.run(argv, cwd=cwd).returncode != 0:
        sys.exit(f"relay: failed: {' '.join(argv)}")


def web_is_stale(web: Path) -> bool:
    """True when the built demo site is missing or older than any of its sources."""
    built = web / "dist" / "index.html"
    if not built.is_file():
        return True
    built_at = built.stat().st_mtime
    sources = [web / "index.html", web / "package.json", *(web / "src").rglob("*")]
    return any(p.is_file() and p.stat().st_mtime > built_at for p in sources)


def build_web(root: Path) -> None:
    web = root / "web"
    if not web_is_stale(web):
        return
    npm = shutil.which("npm")
    if npm is None:
        print("relay: web/dist is out of date and npm is not installed; serving the old build")
        return
    say("Building the demo website")
    if not (web / "node_modules").is_dir():
        run_step([npm, "ci"], web)
    run_step([npm, "run", "build"], web)


def pump(child: subprocess.Popen[str], prefix: str) -> None:
    """Copy a child's output to ours, one prefixed line at a time."""
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(f"{prefix} {line}")
        sys.stdout.flush()


def start(argv: list[str], prefix: str, env: dict[str, str], root: Path) -> subprocess.Popen[str]:
    # Own session: the terminal's Ctrl-C reaches only this launcher, which then stops the
    # children in order instead of every process racing its own shutdown.
    child = subprocess.Popen(
        argv,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    threading.Thread(target=pump, args=(child, prefix), daemon=True).start()
    return child


def public_url_answers(url: str) -> bool:
    """True once ``<url>/healthz`` answers 200 from the internet side of the tunnel."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/healthz", headers={"ngrok-skip-browser-warning": "1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError):
        return False


def start_tunnel(
    public_url: str, port: int, env: dict[str, str], root: Path
) -> subprocess.Popen[str] | None:
    """``ngrok http <port> --url <public_url>``: the URL ElevenLabs calls. None if unavailable."""
    ngrok = shutil.which("ngrok")
    if ngrok is None:
        print("relay: DELEGATOR_PUBLIC_URL is set but ngrok is not installed; voice will not work")
        return None
    say(f"Tunnel {public_url} -> 127.0.0.1:{port}")
    argv = [ngrok, "http", str(port), "--url", public_url, "--log", "stdout"]
    return start([*argv, "--log-format", "logfmt"], "[ngrok]    ", env, root)


def wait_for_tunnel(public_url: str, tunnel: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + TUNNEL_WAIT_S
    while time.monotonic() < deadline and tunnel.poll() is None:
        if public_url_answers(public_url):
            say(f"Voice ready: {public_url}/healthz answers through the tunnel")
            return
        time.sleep(1)
    print(
        f"relay: {public_url}/healthz does not answer through the tunnel; voice will not work "
        "(see the [ngrok] lines above)",
        file=sys.stderr,
    )


def stop(children: list[subprocess.Popen[str]]) -> None:
    """SIGTERM every child, then kill any still running after ``STOP_TIMEOUT_S``."""
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + STOP_TIMEOUT_S
    for child in children:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


class _Stop(Exception):
    """Raised in the main thread on SIGTERM, like KeyboardInterrupt on SIGINT."""


def _raise_stop(signum: int, frame: FrameType | None) -> None:
    raise _Stop


def public_delegator_url() -> str | None:
    """``DELEGATOR_PUBLIC_URL`` (env or ``.env``), or None when it is unset or local."""
    from urllib.parse import urlsplit

    from relay.config import Settings

    url = Settings().delegator_public_url.rstrip("/")
    host = urlsplit(url).hostname or ""
    return None if host in ("", "localhost", "127.0.0.1") else url


def main() -> None:
    parser = argparse.ArgumentParser(prog="relay", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--delegator-port", type=int, default=int(os.environ.get("DELEGATOR_PORT", "8000"))
    )
    parser.add_argument(
        "--executor-port", type=int, default=int(os.environ.get("EXECUTOR_PORT", "8001"))
    )
    args = parser.parse_args()
    root = repo_root()
    os.chdir(root)  # .env and alembic.ini are read relative to the working directory

    for port in (args.delegator_port, args.executor_port):
        if (owner := port_owner(port)) is not None:
            sys.exit(
                f"relay: port {port} is in use by {owner}. Stop it first, or pass "
                "--delegator-port / --executor-port."
            )

    say("Postgres")
    run_step([docker_command(), "compose", "up", "-d", "--wait", "postgres"], root)
    say("Schema migrations")
    run_step([sys.executable, "-m", "alembic", "upgrade", "head"], root)
    build_web(root)

    passcode = os.environ.get("DEMO_PASSCODE") or secrets.token_urlsafe(12)
    env = {
        **os.environ,
        "DEMO_PASSCODE": passcode,
        "DELEGATOR_PORT": str(args.delegator_port),
        "EXECUTOR_PORT": str(args.executor_port),
        # The Delegator dispatches to this executor, wherever .env points.
        "EXECUTOR_URL": f"http://localhost:{args.executor_port}",
        "PYTHONUNBUFFERED": "1",
    }

    signal.signal(signal.SIGTERM, _raise_stop)
    children: list[subprocess.Popen[str]] = []
    code = 0
    try:
        say(f"Executor on http://127.0.0.1:{args.executor_port}")
        children.append(start([sys.executable, "-m", "relay.executor"], "[executor] ", env, root))
        say(f"Delegator on http://127.0.0.1:{args.delegator_port}")
        children.append(start([sys.executable, "-m", "relay.delegator"], "[delegator]", env, root))
        if public_url := public_delegator_url():
            if (tunnel := start_tunnel(public_url, args.delegator_port, env, root)) is not None:
                children.append(tunnel)
                wait_for_tunnel(public_url, tunnel)
        else:
            print("relay: DELEGATOR_PUBLIC_URL is not set; no tunnel, so voice will not work")
        print(
            f"\n    Demo:     http://127.0.0.1:{args.delegator_port}/\n"
            f"    Passcode: {passcode} (only via the tunnel; this PC logs in directly)\n"
            "    Ctrl-C stops everything.\n",
            flush=True,
        )
        while True:
            if any(child.poll() is not None for child in children):
                print("relay: a backend process exited; stopping the rest.", file=sys.stderr)
                code = 1
                break
            time.sleep(0.5)
    except (KeyboardInterrupt, _Stop):
        print()
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # a second Ctrl-C must not cut the stop
        say("Stopping the executor, the Delegator and the tunnel")
        stop(children)
        say("Stopped (Postgres keeps running: docker compose stop postgres)")
    sys.exit(code)


if __name__ == "__main__":
    main()
