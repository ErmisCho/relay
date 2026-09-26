"""Per-session client capability profiles (TASK-35); consumed by routing later (TASK-37).

The store is in-memory and bounded like ``SessionStore``: a lost or evicted profile reads as
cloud-only, which is always correct, merely never local.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict

from fastapi import APIRouter, Depends, FastAPI, Response, status

from relay.client.capabilities import CLOUD_ONLY, CapabilityProfile
from relay.config import Settings
from relay.delegator.auth import bearer_auth

__all__ = ["CLOUD_ONLY", "STORE", "CapabilityProfile", "CapabilityStore", "get_capabilities"]


class CapabilityReport(CapabilityProfile):
    """Wire body of ``POST /v1/capabilities``: the profile keyed by the client's session id."""

    session_id: uuid.UUID


class CapabilityStore:
    """Bounded LRU of session_id -> profile; the least recently used entry is evicted."""

    def __init__(self, max_sessions: int = 256) -> None:
        self._profiles: OrderedDict[uuid.UUID, CapabilityProfile] = OrderedDict()
        self._max = max_sessions

    def put(self, session_id: uuid.UUID, profile: CapabilityProfile) -> None:
        self._profiles[session_id] = profile
        self._profiles.move_to_end(session_id)
        while len(self._profiles) > self._max:
            self._profiles.popitem(last=False)

    def get(self, session_id: uuid.UUID) -> CapabilityProfile:
        profile = self._profiles.get(session_id)
        if profile is None:
            return CLOUD_ONLY
        self._profiles.move_to_end(session_id)
        return profile

    def __len__(self) -> int:
        return len(self._profiles)


STORE = CapabilityStore()


def get_capabilities(session_id: uuid.UUID, store: CapabilityStore = STORE) -> CapabilityProfile:
    """The session's published profile, or cloud-only (``can_run_local=False``) if absent."""
    return store.get(session_id)


def install(app: FastAPI, settings: Settings, store: CapabilityStore = STORE) -> CapabilityStore:
    """Add ``POST /v1/capabilities`` behind the Delegator's bearer secret."""
    router = APIRouter(dependencies=[Depends(bearer_auth(settings.delegator_shared_secret))])

    @router.post("/v1/capabilities", status_code=status.HTTP_204_NO_CONTENT)
    async def publish_capabilities(report: CapabilityReport) -> Response:
        profile = CapabilityProfile.model_validate(report.model_dump(exclude={"session_id"}))
        store.put(report.session_id, profile)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)
    app.state.capabilities = store
    return store
