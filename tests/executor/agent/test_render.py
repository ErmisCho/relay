"""The executor's Markdown rendering and atomic artifact write; pure, no DBOS or models."""

from __future__ import annotations

from pathlib import Path

from relay.executor.agent.agent import ResearchBrief
from relay.executor.agent.runner import artifact_path, render_markdown, write_document


def test_sources_section_lists_each_url_once_and_rewrite_is_idempotent(tmp_path: Path) -> None:
    brief = ResearchBrief(
        title="Tide tables",
        summary="Two sources.",
        body_markdown="Body.",
        sources=["https://a.example/x", "https://b.example/", "https://a.example/x"],
    )
    md = render_markdown(brief)
    assert md.split("## Sources\n\n", 1)[1] == "- <https://a.example/x>\n- <https://b.example/>\n"

    # A re-executed render step overwrites a longer stale file whole and leaves no temp file.
    path = artifact_path(str(tmp_path), "idea", "task")
    write_document(path, "stale partial content " * 20)  # longer than the new document
    write_document(path, md)
    assert path.read_text() == md
    assert [p.name for p in path.parent.iterdir()] == ["task.md"]
