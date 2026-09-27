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
    """Read a fixed Darwin hardware key without a shell or unbounded wait."""
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
    """Prefer the actual Mac chip name over platform.processor()'s generic ``arm``."""
    return (
        _sysctl("machdep.cpu.brand_string") if sys.platform == "darwin" else None
    ) or platform.processor() or platform.machine() or "unknown chip"


def total_memory_bytes() -> int | None:
    """Best-effort total physical memory in bytes, or ``None`` if it can't be read."""
    if sys.platform == "darwin":
        value = _sysctl("hw.memsize")
        if value:
            try:
                if (size := int(value)) > 0:
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


def gpu_description() -> str:
    """Best-effort GPU name from the host's native inventory command."""
    commands = {
        "darwin": ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-detailLevel", "mini"],
        "win32": [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_VideoController).Name -join ', '",
        ],
        "linux": ["lspci"],
    }
    command = commands.get(sys.platform)
    if command is None:
        return "not reported by this operating system"
    try:
        output = subprocess.run(
            command, capture_output=True, text=True, timeout=3, check=False
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "not reported by this operating system"
    if sys.platform == "darwin":
        names = [
            line.split(":", 1)[1].strip()
            for line in output.splitlines()
            if "Chipset Model:" in line
        ]
        return ", ".join(names) or "not reported by macOS"
    if sys.platform == "linux":
        names = [line for line in output.splitlines() if "VGA" in line or "3D controller" in line]
        return ", ".join(names) or "not reported by Linux"
    return output or "not reported by Windows"


def recommend_models(total_gb: float) -> str:
    """One short spoken sentence recommending a local model for ``total_gb`` of memory.

    Kept to one sentence on purpose: in a compound turn ("specs? and the weather?") a
    longer recommendation used up the reply and the model dropped the other answer.
    """
    if total_gb >= 32:
        return "It can comfortably run a 7B local model like qwen2.5:7b-instruct."
    if total_gb >= 16:
        return "It fits a 3B local model like qwen2.5:3b-instruct."
    return "Only a small local model like llama3.2:1b fits well."


class HardwareCapabilitiesTool:
    """``InternalTool`` reporting this machine's hardware profile and a local-LLM recommendation."""

    name = "hardware_capabilities"
    direct_response = True
    description = (
        "Report this machine's hardware profile (chip, memory, GPU) and recommend which local "
        "LLMs fit it."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        chip, gpu, mem_bytes = await asyncio.gather(
            asyncio.to_thread(chip_name),
            asyncio.to_thread(gpu_description),
            asyncio.to_thread(total_memory_bytes),
        )
        if mem_bytes is None:
            return ToolResult(
                f"This machine reports a {chip} chip and GPU: {gpu}, but I can't read its "
                "memory size here."
            )
        total_gb = mem_bytes / _GIB
        return ToolResult(
            f"This machine has a {chip} chip, GPU: {gpu}, and about {total_gb:.0f} GB of memory. "
            + recommend_models(total_gb)
        )
