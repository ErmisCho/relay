"""The ORM models (incl. `Turn.meta` -> column `metadata`) and async session factory work."""

from __future__ import annotations

from datetime import UTC, datetime

from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Commitment, Idea, RouterDecision, Session, Task, Turn


async def test_orm_round_trip(migrated_db_url: str) -> None:
    engine = create_engine(migrated_db_url)
    try:
        async with create_sessionmaker(engine)() as db:
            idea, session = Idea(title="relay"), Session(wake_trigger="hey_jarvis")
            db.add_all([idea, session])
            await db.flush()
            turn = Turn(session_id=session.id, idea_id=idea.id, role="user", text="go",
                        meta={"refused": True})
            commitment = Commitment(
                idea_id=idea.id, goal="g", scope_excludes="x", artifact_kind="document",
                readback_text="r", assent_utterance="yes", assented_at=datetime.now(UTC),
            )
            db.add_all([turn, commitment])
            await db.flush()
            task = Task(commitment_id=commitment.id, kind="research")
            db.add_all([task, RouterDecision(turn_id=turn.id, backend="frontier")])
            await db.commit()

            for obj in (idea, turn, task):
                await db.refresh(obj)
            assert turn.meta == {"refused": True}
            assert (idea.status, task.status) == ("exploring", "queued")
    finally:
        await engine.dispose()
