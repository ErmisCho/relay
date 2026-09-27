"""Commitment protocol (SPEC §6): the gate may only ever propose; dispatch needs spoken assent.

Register in ``relay.delegator.wiring``: ``ProposeCommitmentTool`` and ``DispatchTaskTool`` in the
tool registry, ``CommitmentHook`` in the hooks (after ``ScopeHook``).
"""

from relay.delegator.commitment.classify import (
    ASSENT_SYSTEM_PROMPT,
    AssentLabel,
    AssentResult,
    classify_assent,
    precheck,
)
from relay.delegator.commitment.protocol import (
    ASSENT_KEY,
    AssentRecord,
    CommitmentHook,
    DispatchTaskTool,
    ProposeCommitmentTool,
    authorise,
)
from relay.delegator.commitment.readback import build_readback, readback_delivered

__all__ = [
    "ASSENT_KEY",
    "ASSENT_SYSTEM_PROMPT",
    "AssentLabel",
    "AssentRecord",
    "AssentResult",
    "CommitmentHook",
    "DispatchTaskTool",
    "ProposeCommitmentTool",
    "authorise",
    "build_readback",
    "classify_assent",
    "precheck",
    "readback_delivered",
]
