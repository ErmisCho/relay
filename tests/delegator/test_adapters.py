"""OpenAI <-> Anthropic translation."""

from __future__ import annotations

from types import SimpleNamespace as NS

from relay.delegator.adapters.openai_compat import (
    AnthropicStreamTranslator,
    openai_messages_to_anthropic,
)


def test_messages_hoist_system_and_merge_tool_results() -> None:
    system, messages = openai_messages_to_anthropic(
        [
            {"role": "system", "content": "persona"},
            {"role": "assistant", "content": "Hey!"},
            {"role": "user", "content": [{"type": "text", "text": "look it up"}]},
            {"role": "system", "content": "note"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
                    {"id": "b", "type": "function",
                     "function": {"name": "y", "arguments": '{"k":1}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
        ]
    )
    assert system == "persona\n\nnote"
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[3]["content"][1] == {"type": "tool_use", "id": "b", "name": "y",
                                         "input": {"k": 1}}
    assert [b["tool_use_id"] for b in messages[4]["content"]] == ["a", "b"]


def test_stream_events_number_tool_calls_independently_of_text_blocks() -> None:
    tr = AnthropicStreamTranslator()
    events = [
        NS(type="content_block_start", index=0, content_block=NS(type="text")),
        NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="Sure.")),
        NS(type="text", text="Sure."),  # SDK convenience event: must not duplicate text
        NS(type="content_block_start", index=1,
           content_block=NS(type="tool_use", id="tu_1", name="end_call")),
        NS(type="content_block_delta", index=1,
           delta=NS(type="input_json_delta", partial_json='{"reason":')),
        NS(type="content_block_delta", index=1,
           delta=NS(type="input_json_delta", partial_json='"bye"}')),
        NS(type="message_delta", delta=NS(stop_reason="tool_use")),
    ]
    deltas = [d for d in (tr.translate(e) for e in events) if d is not None]
    assert "".join(d.content or "" for d in deltas) == "Sure."
    frags = [tc for d in deltas for tc in d.tool_calls]
    assert {tc.index for tc in frags} == {0}
    assert (frags[0].id, frags[0].name) == ("tu_1", "end_call")
    assert "".join(tc.arguments or "" for tc in frags) == '{"reason":"bye"}'
    assert deltas[-1].finish_reason == "tool_calls"
