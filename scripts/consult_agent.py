import logging
import json
import httpx
import google.auth
import google.auth.transport.requests
import hubscape_adk

logger = logging.getLogger(__name__)

async def consultAgent(agentId: str, query: str) -> str:
    """
    Consults a specialized subagent (e.g. todo_agent, knowledge_agent, admin_ui_agent).
    
    Args:
        agentId: The ID of the target subagent.
        query: The prompt or instruction for the subagent.
    """
    try:
        ctx = hubscape_adk.get_context()
        raw_ctx = ctx.raw_context
        accessible_agents = raw_ctx.get("accessible_agents", [])
        
        # 1. Resolve subagent in whitelist
        target_agent = None
        for agent in accessible_agents:
            if agent.get("id") == agentId:
                target_agent = agent
                break
                
        if not target_agent:
            return f"Error: Agent '{agentId}' is not accessible or not whitelisted."
            
        resource_name = target_agent.get("geap_resource_name")
        if not resource_name:
            return f"Error: Agent '{agentId}' does not have a valid remote resource name."
            
        # 2. Get GCP access token
        credentials, project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        token = credentials.token
        
        location = "us-central1"
        url = f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}:query"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "input": {
                "question": query,
                "context": raw_ctx
            }
        }
        
        logger.info(f"📡 Consulting remote GEAP subagent: {agentId} ({resource_name})")
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=90.0)
            response.raise_for_status()
            data = response.json()
            subagent_output = data.get("output", "")
            
        # 3. Intercept directives and map to client actions
        try:
            parsed = json.loads(subagent_output)
            if isinstance(parsed, dict):
                directive = parsed.get("directive")
                target_tool = parsed.get("target_tool")
                parameters = parsed.get("parameters") or {}
                message = parsed.get("message") or ""
                
                if directive == "execute_host_tool":
                    if target_tool == "openAdminWidget":
                        ctx.actions.append({
                            "type": "OPEN_ADMIN_WIDGET",
                            "payload": {
                                "widgetType": parameters.get("widgetType")
                            }
                        })
                        return message or f"Opening the {parameters.get('widgetType')} widget."
                        
                    elif target_tool == "suggestQueries":
                        ctx.actions.append({
                            "type": "SET_SUGGESTIONS",
                            "queries": parameters.get("queries") or []
                        })
                        return message
                        
                    elif target_tool == "switchHub":
                        ctx.actions.append({
                            "type": "SWITCH_HUB",
                            "payload": {
                                "hubId": parameters.get("hubId")
                            }
                        })
                        return message or "Switching hub workspace."
                        
                    elif target_tool == "openExternalLink":
                        ctx.actions.append({
                            "type": "OPEN_EXTERNAL_LINK",
                            "payload": {
                                "url": parameters.get("url")
                            }
                        })
                        return message or f"Opening external link: {parameters.get('url')}"
                        
                    elif target_tool == "endCall":
                        ctx.actions.append({
                            "type": "END_CALL"
                        })
                        return message or "Call ended."
                        
                elif directive == "respond_to_user":
                    return message
        except Exception:
            # If not a JSON string, propagate the raw output verbatim
            pass
            
        return subagent_output
        
    except Exception as e:
        logger.error(f"Error consulting subagent {agentId}: {e}", exc_info=True)
        return f"Error: Failed to consult subagent '{agentId}': {str(e)}"
