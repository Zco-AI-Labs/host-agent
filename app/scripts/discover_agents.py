import logging
import hubscape_adk

logger = logging.getLogger(__name__)

def discover_agents(query: str = None) -> list:
    """
    Search and discover registered A2A subagents whitelisted for the active user session.
    
    Args:
        query: Optional search keyword to filter agents by name or description.
    """
    try:
        ctx = hubscape_adk.get_context()
        raw_ctx = ctx.raw_context
        accessible_agents = raw_ctx.get("accessible_agents", [])
        
        results = []
        for agent in accessible_agents:
            name = agent.get("name") or ""
            desc = agent.get("description") or ""
            a2a_url = agent.get("a2aUrl") or ""
            
            # Use computed fallback from geap_resource_name if a2aUrl is not present
            if not a2a_url and agent.get("geap_resource_name"):
                resource_name = agent.get("geap_resource_name")
                location = "us-central1"
                if "/" in resource_name:
                    parts = resource_name.split("/")
                    if len(parts) > 3:
                        location = parts[3]
                a2a_url = f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}"
            
            if query:
                q = query.lower()
                if q not in name.lower() and q not in desc.lower():
                    continue
            
            results.append({
                "id": agent.get("id"),
                "name": name,
                "description": desc,
                "a2aUrl": a2a_url
            })
            
        return results
    except Exception as e:
        logger.error(f"Error in discover_agents: {e}", exc_info=True)
        return [{"error": f"Failed to search registry: {str(e)}"}]
