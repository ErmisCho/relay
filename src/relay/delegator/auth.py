"""Shared-secret bearer auth for the custom-LLM endpoint."""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, status

from relay.config import Settings

DEV_SECRET = "dev-secret-change-me"
ALLOW_DEV_SECRET_ENV = "RELAY_ALLOW_DEV_SECRET"


def check_secret_is_safe(settings: Settings) -> None:
    """Refuse to start with an empty secret, or the dev placeholder unless explicitly allowed.

    The public URL is not a reliable signal: the usual tunnel setup forwards to
    127.0.0.1 while ``DELEGATOR_PUBLIC_URL`` stays at localhost, so the placeholder
    needs an explicit ``RELAY_ALLOW_DEV_SECRET=1`` opt-in.
    """
    if not settings.delegator_shared_secret:
        raise RuntimeError("DELEGATOR_SHARED_SECRET must not be empty")
    if (
        settings.delegator_shared_secret == DEV_SECRET
        and os.environ.get(ALLOW_DEV_SECRET_ENV) != "1"
    ):
        raise RuntimeError(
            "DELEGATOR_SHARED_SECRET is still the dev placeholder; set a real secret "
            f"(or {ALLOW_DEV_SECRET_ENV}=1 for purely local development)"
        )


def bearer_auth(secret: str) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency: 401 unless ``Authorization: Bearer <secret>`` matches."""
    expected = secret.encode("utf-8")

    async def require_bearer(authorization: str | None = Header(default=None)) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode("utf-8"), expected
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_bearer
