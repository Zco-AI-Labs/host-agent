from app.core import hubscape_adk

@hubscape_adk.require_tool_privilege
def suggestQueries(queries: list[str]) -> dict:
    """
    Renders interactive suggestion bubbles in the client user interface for ambiguity resolution.
    
    Args:
        queries: A list of suggested search terms or queries.
    """
    # Sanitize & Cap Query Lengths (Max 8 words, 45 chars max)
    capped_queries = []
    for q in (queries or []):
        if not isinstance(q, str):
            continue
        cleaned = q.strip().strip('"').strip("'")
        words = cleaned.split()
        if len(words) > 8:
            cleaned = " ".join(words[:8])
        if len(cleaned) > 45:
            cleaned = cleaned[:45].strip()
        if cleaned:
            capped_queries.append(cleaned)

    ctx = hubscape_adk.get_context()
    ctx.actions.append({
        "type": "SET_SUGGESTIONS",
        "queries": capped_queries[:3]
    })
    return {
        "status": "success",
        "message": f"Successfully set suggested queries: {', '.join(queries)}"
    }
