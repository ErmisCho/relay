"""Server-sent-event framing for the OpenAI streaming protocol."""

from __future__ import annotations

import json
from typing import Any

SSE_DONE = b"data: [DONE]\n\n"


def sse_frame(payload: dict[str, Any]) -> bytes:
    """``data: <compact one-line JSON>\\n\\n``."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return b"data: " + body.encode("utf-8") + b"\n\n"
