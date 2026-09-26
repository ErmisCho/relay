"""Client capability detection and publishing (TASK-35).

Cloud is the default; local inference is an opportunistic optimisation. Everything here is
best-effort: the probe never raises and is bounded in time, and publishing runs in the
background so it can never delay or block the wake -> session path. A device that probes
nothing (or cannot reach the Delegator) stays fully functional and simply routes to cloud.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import importlib.util
import logging
import platform
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict

from relay.config import Settings, parse_model_ref

if TYPE_CHECKING:  # the Delegator imports this module; keep the audio stack out of it
    from relay.client.listener import SessionStore

log = logging.getLogger(__name__)

OLLAMA_TIMEOUT_S = 1.0
MEM_TIMEOUT_S = 1.0
PUBLISH_TIMEOUT_S = 3.0
CAPABILITIES_PATH = "/v1/capabilities"

# Settings fields whose Ollama models count as "local models"; the first one installed wins.
# router_model leads because small_local turns (TASK-37) run on the router-class model.
LOCAL_MODEL_FIELDS = (
    "router_model",
    "delegator_model",
    "delegator_fallback_model",
    "assent_model",
    "ready_model",
    "summary_model",
    "research_easy_model",
    "research_fallback_model",
    "research_model",
    "research_hard_model",
    "research_hard_fallback_model",
)


class CapabilityProfile(BaseModel):
    """What this device can run locally. The all-defaults instance means cloud-only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    can_run_laya_mlx: bool = False
    can_run_local: bool = False
    local_model: str | None = None
    local_base_url: str | None = None
    free_mem_gb: float | None = None


CLOUD_ONLY = CapabilityProfile()


def _ollama_name(name: str) -> str:
    """Ollama lists `gemma4` as `gemma4:latest`; compare in that canonical form."""
    return name if ":" in name else f"{name}:latest"


def configured_local_models(settings: Settings) -> list[str]:
    """Ollama model names referenced by settings, in preference order, deduplicated."""
    models: list[str] = []
    for field in LOCAL_MODEL_FIELDS:
        ref = getattr(settings, field, None)
        if not isinstance(ref, str) or not ref.strip():
            continue
        try:
            provider, model = parse_model_ref(ref)
        except ValueError:
            continue
        if provider == "ollama" and _ollama_name(model) not in models:
            models.append(_ollama_name(model))
    return models


def ollama_root(base_url: str) -> str:
    """`http://host:11434/v1` (the OpenAI-compatible base in settings) -> `http://host:11434`."""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def mlx_available() -> bool:
    # find_spec, not import: importing mlx initialises Metal, which is slow and not needed here.
    try:
        return importlib.util.find_spec("mlx") is not None
    except (ImportError, ValueError):
        return False


async def probe_ollama(
    base_url: str,
    wanted: list[str],
    *,
    timeout_s: float = OLLAMA_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """Return the first wanted model the daemon has installed, or None. Never raises."""
    if not wanted:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            resp = await asyncio.wait_for(
                client.get(f"{ollama_root(base_url)}/api/tags"), timeout_s
            )
            resp.raise_for_status()
            data = resp.json()
        installed = {
            _ollama_name(str(m.get("name") or m.get("model") or ""))
            for m in data.get("models", [])
            if isinstance(m, dict)
        }
    except Exception as exc:  # daemon down, slow, or not Ollama: all mean "no local"
        log.info("ollama probe at %s: unavailable (%s)", base_url, type(exc).__name__)
        return None
    return next((m for m in wanted if m in installed), None)


def _meminfo_available_gb(text: str) -> float | None:
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(match.group(1)) / 1024**2 if match else None


def _vm_stat_available_gb(text: str) -> float | None:
    page = re.search(r"page size of (\d+) bytes", text)
    if page is None:
        return None
    pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        found = re.search(rf"^{label}:\s+(\d+)", text, re.MULTILINE)
        if found:
            pages += int(found.group(1))
    return pages * int(page.group(1)) / 1024**3


def free_memory_gb() -> float | None:
    """Available RAM in GiB via psutil when installed, else a per-OS stdlib fallback."""
    try:
        psutil: Any = importlib.import_module("psutil")
        return float(psutil.virtual_memory().available) / 1024**3
    except Exception:
        pass
    try:
        if sys.platform.startswith("linux"):
            return _meminfo_available_gb(Path("/proc/meminfo").read_text())
        if sys.platform == "darwin":
            out = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=MEM_TIMEOUT_S, check=True
            ).stdout
            return _vm_stat_available_gb(out)
        if sys.platform == "win32":
            return _windows_available_gb()
    except Exception:
        log.info("free-memory probe failed", exc_info=True)
    return None


def _windows_available_gb() -> float | None:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes requires a plain list here
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    windll: Any = getattr(ctypes, "windll", None)
    if windll is None or not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return float(status.ullAvailPhys) / 1024**3


async def probe(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> CapabilityProfile:
    """Probe this device. Never raises; returns within ~max(OLLAMA, MEM) timeouts."""
    try:
        base_url = settings.ollama_base_url

        async def memory() -> float | None:
            try:
                return await asyncio.wait_for(asyncio.to_thread(free_memory_gb), MEM_TIMEOUT_S)
            except Exception:
                return None

        local_model, free_mem = await asyncio.gather(
            probe_ollama(base_url, configured_local_models(settings), transport=transport),
            memory(),
        )
        return CapabilityProfile(
            can_run_laya_mlx=is_apple_silicon() and mlx_available(),
            can_run_local=local_model is not None,
            local_model=local_model,
            local_base_url=base_url if local_model is not None else None,
            free_mem_gb=round(free_mem, 2) if free_mem is not None else None,
        )
    except Exception:
        log.warning("capability probe failed; reporting cloud-only", exc_info=True)
        return CLOUD_ONLY


async def publish(
    profile: CapabilityProfile,
    session_id: uuid.UUID,
    *,
    delegator_url: str,
    secret: str,
    timeout_s: float = PUBLISH_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """POST the profile to the Delegator for ``session_id``; True on 2xx. Never raises."""
    body = {"session_id": str(session_id), **profile.model_dump()}
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            resp = await client.post(
                delegator_url.rstrip("/") + CAPABILITIES_PATH,
                json=body,
                headers={"Authorization": f"Bearer {secret}"},
            )
    except Exception as exc:
        log.info("capability publish for %s failed (%s)", session_id, type(exc).__name__)
        return False
    if resp.is_success:
        return True
    log.warning("capability publish for %s rejected: HTTP %d", session_id, resp.status_code)
    return False


class CapabilityPublishingStore:
    """Wraps the listener's ``SessionStore``: every session start re-probes and publishes.

    ``create`` is the hook the listener already calls once per wake, before the voice session
    starts. The probe and POST run as a background task, so a slow or failing probe, a down
    Delegator, or a raising inner store can never block or break session start.
    """

    def __init__(
        self,
        inner: SessionStore,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._inner = inner
        self._settings = settings
        self._transport = transport
        self._tasks: set[asyncio.Task[None]] = set()
        self.last: CapabilityProfile = CLOUD_ONLY

    def _spawn(self, coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _probe(self) -> CapabilityProfile:
        self.last = await probe(self._settings, transport=self._transport)
        return self.last

    async def _startup(self) -> None:
        profile = await self._probe()
        log.info("capabilities at startup: %s", profile.model_dump())

    async def _probe_and_publish(self, session_id: uuid.UUID) -> None:
        try:
            profile = await self._probe()
            await publish(
                profile,
                session_id,
                delegator_url=self._settings.delegator_public_url,
                secret=self._settings.delegator_shared_secret,
                transport=self._transport,
            )
        except Exception:  # background task: log, never propagate
            log.warning("capability publish for %s failed", session_id, exc_info=True)

    def warm(self) -> asyncio.Task[None]:
        """Probe once at client startup, in the background (before listening starts)."""
        return self._spawn(self._startup())

    async def create(self, session_id: uuid.UUID, wake_trigger: str) -> None:
        self._spawn(self._probe_and_publish(session_id))
        await self._inner.create(session_id, wake_trigger)

    async def end(self, session_id: uuid.UUID, reason: str) -> None:
        await self._inner.end(session_id, reason)

    async def end_stale(self, idle_s: float) -> int:
        return await self._inner.end_stale(idle_s)

    async def aclose(self, timeout_s: float = PUBLISH_TIMEOUT_S) -> None:
        """Give in-flight publishes a bounded chance to finish, then cancel the rest."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout_s)
        for task in pending:
            task.cancel()
