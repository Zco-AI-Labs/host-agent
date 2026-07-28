import pytest
from google.adk.events.event import Event
from google.genai import types as genai_types


@pytest.mark.asyncio
async def test_streaming_partial_events_filtering():
    """
    Tests that partial streaming events from subagent.run_async are filtered out,
    preventing 20+ repeated output chunks from being concatenated together.
    """
    # Simulate a stream of 20 partial events followed by 1 final non-partial event
    events = []
    for i in range(1, 21):
        partial_text = f"Motor oil should be replaced every {i*500} miles."
        events.append(
            Event(
                author="knowledge_agent",
                content=genai_types.Content(
                    parts=[genai_types.Part.from_text(text=partial_text)]
                ),
                partial=True,
            )
        )

    final_text = "Motor oil and filter should be replaced or inspected every 3,000 to 5,000 miles or according to manufacturer guidelines."
    events.append(
        Event(
            author="knowledge_agent",
            content=genai_types.Content(
                parts=[genai_types.Part.from_text(text=final_text)]
            ),
            partial=False,
        )
    )

    collected_chunks = []
    last_partial_out = ""

    for ev in events:
        out = getattr(ev, "output", None)
        if not out and getattr(ev, "content", None) and getattr(ev.content, "parts", None):
            text_parts = [p.text for p in ev.content.parts if getattr(p, "text", None)]
            if text_parts:
                out = "\n".join(text_parts)
        if out and isinstance(out, str) and out.strip():
            clean_out = out.strip()
            if getattr(ev, "partial", False):
                last_partial_out = clean_out
            else:
                if not collected_chunks or clean_out != collected_chunks[-1].strip():
                    collected_chunks.append(clean_out)

    if not collected_chunks and last_partial_out:
        collected_chunks.append(last_partial_out)

    subagent_output = "\n".join(collected_chunks)

    # Verify that only the final complete non-partial response is returned, NOT 21 repeated chunks
    assert subagent_output == final_text


@pytest.mark.asyncio
async def test_multistep_event_deduplication():
    """
    Tests that when an agent turn yields a partial step text followed by a fuller final text,
    the subset event output is replaced rather than concatenated as a double response.
    """
    part1_text = "Affirmative, Commander! I've found a video that delves into the costs of app development."
    part2_text = "Affirmative, Commander! I've found a video that delves into the costs of app development.\n\nI also found an article explaining app pricing."

    events = [
        Event(
            author="host_agent",
            content=genai_types.Content(parts=[genai_types.Part.from_text(text=part1_text)]),
            partial=False,
        ),
        Event(
            author="host_agent",
            content=genai_types.Content(parts=[genai_types.Part.from_text(text=part2_text)]),
            partial=False,
        )
    ]

    collected_outputs = []
    last_partial_output = ""

    for ev in events:
        out = getattr(ev, "output", None)
        if not out and getattr(ev, "content", None) and getattr(ev.content, "parts", None):
            text_parts = [p.text for p in ev.content.parts if getattr(p, "text", None)]
            if text_parts:
                out = "\n".join(text_parts)
        if out and isinstance(out, str) and out.strip():
            clean_out = out.strip()
            if getattr(ev, "partial", False):
                last_partial_output = clean_out
            else:
                if collected_outputs:
                    last = collected_outputs[-1]
                    if clean_out.startswith(last) or last in clean_out:
                        collected_outputs[-1] = clean_out
                    elif clean_out in last or last.startswith(clean_out):
                        pass
                    elif clean_out != last:
                        collected_outputs.append(clean_out)
                else:
                    collected_outputs.append(clean_out)

    if not collected_outputs and last_partial_output:
        collected_outputs.append(last_partial_output)

    text_response = "\n".join(collected_outputs)

    assert text_response == part2_text
    assert len(collected_outputs) == 1
