import logging
import json
import httpx
import google.auth
import google.auth.transport.requests
import hubscape_adk
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.events.event import Event as AdkEvent
from google.genai import types as genai_types
from google.adk.sessions.session import Session
from google.adk.agents.invocation_context import InvocationContext

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
        
        # Prevent infinite agent-to-agent delegation loops (max depth = 3)
        current_depth = raw_ctx.get("depth", 0)
        max_depth = 3
        if current_depth >= max_depth:
            return f"Error: Maximum agent delegation depth of {max_depth} exceeded. Aborting call to prevent infinite loops."
            
        accessible_agents = raw_ctx.get("accessible_agents", [])
        
        # 1. Resolve subagent in whitelist
        def normalize(s: str) -> str:
            return "".join(c for c in s.lower() if c.isalnum())

        target_agent = None
        normalized_query_id = normalize(agentId)
        for agent in accessible_agents:
            aid = agent.get("id") or ""
            aname = agent.get("name") or ""
            if aid == agentId or normalize(aid) == normalized_query_id or normalize(aname) == normalized_query_id:
                target_agent = agent
                break
                
        if not target_agent:
            return f"Error: Agent '{agentId}' is not accessible or not whitelisted."
            
        # Resolve A2A URL or fallback to computing it from geap_resource_name
        a2a_url = target_agent.get("a2aUrl")
        resource_name = target_agent.get("geap_resource_name")
        if not a2a_url and resource_name:
            location = "us-central1"
            if "/" in resource_name:
                parts = resource_name.split("/")
                if len(parts) > 3:
                    location = parts[3]
            a2a_url = f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}"
            if target_agent.get("type") == "A2A":
                a2a_url = f"{a2a_url}/a2a"
            
        if not a2a_url:
            return f"Error: Agent '{agentId}' does not have a valid A2A URL or remote resource name."
            
        # Ensure we use v1beta1 routing for A2A card resolution and predict queries
        card_url = a2a_url
        if "/v1/" in card_url:
            card_url = card_url.replace("/v1/", "/v1beta1/")
        if "/a2a" not in card_url:
            card_url = card_url.rstrip("/") + "/a2a"
        if not card_url.endswith("/v1/card"):
            card_url = card_url.rstrip("/") + "/v1/card"
            
        # 2. Get GCP access token
        def get_gcp_access_token() -> str:
            import httpx as httpx_sync
            # Try to fetch from the local metadata server (correct for remote container)
            try:
                meta_url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
                resp = httpx_sync.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=2.0)
                if resp.status_code == 200:
                    tok = resp.json().get("access_token")
                    if tok:
                        return tok
            except Exception:
                pass

            # Fallback to google.auth.default (correct for local development)
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
            return credentials.token

        token = get_gcp_access_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        httpx_client = httpx.AsyncClient(headers=headers, timeout=90.0)
        
        logger.info(f"📡 Consulting remote A2A subagent via ADK client: {agentId} ({card_url})")
        
        # Request metadata provider to securely propagate RBAC context and increment call depth
        def request_meta_provider(invocation_context, a2a_message):
            return {
                "userId": ctx.auth.get_user_id(),
                "user_id": ctx.auth.get_user_id(),
                "orgId": ctx.auth.org_id,
                "org_id": ctx.auth.org_id,
                "hubId": ctx.auth.hub_id,
                "hub_id": ctx.auth.hub_id,
                "mode": raw_ctx.get("mode"),
                "accessible_agents": accessible_agents,
                "depth": current_depth + 1
            }

        # Normalize the agent ID to a valid Python identifier
        import re
        valid_name = re.sub(r'[^a-zA-Z0-9_]', '_', agentId)
        if not valid_name[0].isalpha() and valid_name[0] != '_':
            valid_name = '_' + valid_name

        # Instantiate the Remote A2A Agent using the ADK Client
        subagent = RemoteA2aAgent(
            name=valid_name,
            agent_card=card_url,
            httpx_client=httpx_client,
            a2a_request_meta_provider=request_meta_provider
        )
        
        # Construct dummy session context containing the user's specific query
        adk_event = AdkEvent(
            author="user",
            content=genai_types.Content(parts=[genai_types.Part.from_text(text=query)])
        )
        dummy_session = Session(
            id="dummy_session_id",
            app_name="consult_agent",
            user_id="dummy_user",
            state={},
            events=[adk_event]
        )
        from google.adk.sessions.in_memory_session_service import InMemorySessionService
        parent_ctx = InvocationContext(
            invocation_id="dummy_invocation_id",
            branch="0",
            session=dummy_session,
            session_service=InMemorySessionService()
        )
        
        subagent_output = ""
        async for ev in subagent.run_async(parent_context=parent_ctx):
            if ev.output:
                subagent_output += ev.output
            elif ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if part.text:
                        subagent_output += part.text
            
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
                        
                    elif target_tool == "openAgentWidget":
                        ctx.actions.append({
                            "type": "OPEN_AGENT_WIDGET",
                            "payload": {
                                "id": agentId,
                                "widgetId": parameters.get("widgetId"),
                                "widgetConfig": parameters.get("widgetConfig"),
                                "data": parameters.get("data") or {},
                                "styling": parameters.get("styling") or {},
                                "userPreferences": parameters.get("userPreferences") or {}
                            }
                        })
                        return message or f"Displaying agent widget: {parameters.get('widgetId')}"
                        
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
