from app.core import hubscape_adk

@hubscape_adk.require_tool_privilege
def suggestQueries(queries: list[str]) -> dict:
    """
    Renders interactive suggestion bubbles in the client user interface for ambiguity resolution.
    
    Args:
        queries: A list of suggested search terms or queries.
    """
    ctx = hubscape_adk.get_context()
    ctx.actions.append({
        "type": "SET_SUGGESTIONS",
        "queries": queries
    })
    return {
        "status": "success",
        "message": f"Successfully set suggested queries: {', '.join(queries)}"
    }
