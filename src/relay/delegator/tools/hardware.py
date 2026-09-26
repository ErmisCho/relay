"""``hardware_capabilities`` internal tool: local hardware profile + a local-LLM recommendation.

Deliberate scope expansion beyond SPEC.md's two verticals (user decision,
2026-09-26): answers directly in conversation, never through the commitment
protocol — there is no artifact to dispatch for "what can my machine run".
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import subprocess
import sys
from typing import Any

from relay.delegator.contracts import ToolContext, ToolResult

_GIB = 1024**3


def _sysctl(key: str) -> str | None:
    """Read a fixed Darwin hardware key; never invoke a shell or wait indefinitely."""
    try:
        return (
            subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", key],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            ).strip()
            or None
        )
    except (OSError, subprocess.SubprocessError):
        return None


def chip_name() -> str:
    """Prefer the actual Mac chip name over platform.processor()'s generic 'arm'."""
    if sys.platform == "darwin":
        brand = _sysctl("machdep.cpu.brand_string")
        if brand:
            return brand
    return platform.processor() or platform.machine() or "unknown chip"


def total_memory_bytes() -> int | None:
    """Best-effort total physical memory in bytes, or ``None`` if it can't be read."""
    if sys.platform == "darwin":
        value = _sysctl("hw.memsize")
        if value:
            try:
                size = int(value)
                if size > 0:
                    return size
            except ValueError:
                pass
    sysconf = getattr(os, "sysconf", None)
    if sysconf is not None:
        try:
            return int(sysconf("SC_PAGE_SIZE")) * int(sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError):
            pass
    if sys.platform == "win32":
        return _windows_total_memory_bytes()
    return None


def _windows_total_memory_bytes() -> int | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
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

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    windll = getattr(ctypes, "windll", None)
    if windll is not None and windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return int(stat.ullTotalPhys)
    return None


def recommend_models(total_gb: float) -> str:
    """One spoken sentence recommending local models for ``total_gb`` of memory.

    Tiers are deliberately coarse: the point is a usable default, not a benchmark.
    """
    if total_gb >= 32:
        return (
            "You have plenty of headroom - qwen2.5:7b-instruct is a solid default for "
            "general turns, with llama3.2:3b as a faster fallback if latency ever matters more "
            "than quality."
        )
    if total_gb >= 16:
        return (
            "qwen2.5:3b-instruct or llama3.2:3b both fit comfortably; a 7B model will run but "
            "leaves less headroom for everything else."
        )
    return (
        "Memory is tight for a good local experience - llama3.2:1b or gemma3:1b are the "
        "realistic options; anything larger risks swapping."
    )


class HardwareCapabilitiesTool:
    """``InternalTool`` reporting this machine's hardware profile and a local-LLM recommendation."""

    name = "hardware_capabilities"
    description = (
        "Read the CPU/chip and physical RAM of the machine running Relay's backend. "
        "Use for questions about this PC's specs, hardware, processor, memory, or which "
        "local LLMs fit. Does not inspect a separate phone or remote client."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # A subprocess probe must not block the audio/session event loop.
        chip, mem_bytes = await asyncio.gather(
            asyncio.to_thread(chip_name), asyncio.to_thread(total_memory_bytes)
        )
        if mem_bytes is None:
            return ToolResult(
                f"The machine running Relay reports a {chip} chip, "
                "but I can't read its memory size here."
            )
        total_gb = mem_bytes / _GIB
        return ToolResult(
            f"The machine running Relay has a {chip} chip "
            f"with about {total_gb:.0f} GB of memory. " + recommend_models(total_gb)
        )
