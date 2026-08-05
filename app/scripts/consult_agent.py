import logging
import json
import httpx
import google.auth
import google.auth.transport.requests
from app.core import hubscape_adk
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.events.event import Event as AdkEvent
from google.genai import types as genai_types
from google.adk.sessions.session import Session
from google.adk.agents.invocation_context import InvocationContext

logger = logging.getLogger(__name__)

def extract_dynamic_entity(persona_text: str, hub_name: str) -> str:
    """Extracts the primary business/venue entity dynamically from any hub persona or name (0% hardcoded)."""
    generic_names = {"platform", "hubscape", "events hub", "main hub", "portal", "support", "workspace"}
    if hub_name and isinstance(hub_name, str) and hub_name.lower().strip() not in generic_names:
        return hub_name.strip()

    if persona_text and isinstance(persona_text, str):
        import re
        stopwords = {"you", "are", "the", "ai", "assistant", "official", "concierge", "manager", "host", "for", "in", "here", "to", "help", "welcome", "your", "goal", "is", "provide", "warm", "enthusiastic", "guests", "attending"}
        words = re.findall(r"[A-Z][a-zA-Z0-9']*", persona_text[:250])
        entity_words = [w for w in words if w.lower() not in stopwords]
        if entity_words:
            return " ".join(entity_words[:3])

    return ""


@hubscape_adk.require_tool_privilege
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
        
        # Universal Dynamic Query Enrichment for Knowledge Search (0% Hardcoded)
        if agentId == "knowledge_agent":
            persona = (
                raw_ctx.get("custom_persona") or
                raw_ctx.get("persona") or
                raw_ctx.get("identityPrompt") or
                raw_ctx.get("system_instruction") or ""
            )
            hub_name = raw_ctx.get("hubName") or raw_ctx.get("hub_name") or ""
            entity_name = extract_dynamic_entity(persona, hub_name)

            if entity_name and entity_name.lower() not in query.lower():
                enriched_query = f"{entity_name} {query}"
                logger.info(f"💡 Dynamic Entity Enrichment: '{query}' -> '{enriched_query}' (Extracted: '{entity_name}')")
                query = enriched_query
        
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
            
        # NOTE: Using v1beta1 specifically for the A2A handshake gateway because 
        # Vertex AI Reasoning Engine's A2A routing endpoints (e.g. /a2a/v1/card)
        # are not exposed on the GA /v1/ endpoints (returning a 404 Not Found).
        card_url = a2a_url
        if "/v1/" in card_url:
            card_url = card_url.replace("/v1/", "/v1beta1/")
        if "/a2a" not in card_url:
            card_url = card_url.rstrip("/") + "/a2a"
        if not card_url.endswith("/v1/card"):
            card_url = card_url.rstrip("/") + "/v1/card"
            
        # 2. Get GCP access token
        def get_gcp_access_token() -> str:
            # Natively resolves and exchanges Workload Identity / ADC credentials
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
        
        logger.info(f"📡 Consulting remote A2A subagent via ADK client: {agentId} ({card_url})")
        
        session_id = raw_ctx.get("sessionId") or raw_ctx.get("session_id")
        if not session_id:
            logger.warning("⚠️ No explicit sessionId found in raw_ctx; constructing fallback session ID.")
            session_id = f"session_{ctx.auth.get_user_id() or 'guest'}_{ctx.auth.hub_id or 'platform'}"


        # Request metadata provider to securely propagate RBAC context, session ID, temporal sensors, and increment call depth
        def request_meta_provider(invocation_context, a2a_message):
            return {
                "userId": ctx.auth.get_user_id(),
                "user_id": ctx.auth.get_user_id(),
                "orgId": ctx.auth.org_id,
                "org_id": ctx.auth.org_id,
                "hubId": ctx.auth.hub_id,
                "hub_id": ctx.auth.hub_id,
                "sessionId": session_id,
                "session_id": session_id,
                "workspaceType": raw_ctx.get("workspaceType") or ("hub" if ctx.auth.hub_id else "organization"),
                "workspaceId": raw_ctx.get("workspaceId") or ctx.auth.hub_id or ctx.auth.org_id,
                "mode": raw_ctx.get("mode"),
                "accessible_agents": accessible_agents,
                "depth": current_depth + 1,
                "backend_url": raw_ctx.get("backend_url"),
                "capability_token": raw_ctx.get("capability_token"),
                "storageBucket": raw_ctx.get("storageBucket"),
                "user_timezone": raw_ctx.get("user_timezone"),
                "current_local_datetime": raw_ctx.get("current_local_datetime"),
                "day_of_week": raw_ctx.get("day_of_week"),
                "current_iso_timestamp": raw_ctx.get("current_iso_timestamp")
            }

        if not agentId or not str(agentId).strip():
            return json.dumps({"text": "Failed to consult subagent: agentId was empty or invalid."})

        # Normalize the agent ID to a valid Python identifier
        import re
        valid_name = re.sub(r'[^a-zA-Z0-9_]', '_', str(agentId))
        if valid_name and not valid_name[0].isalpha() and valid_name[0] != '_':
            valid_name = '_' + valid_name

        # Construct parent session context containing the active session_id and user query
        adk_event = AdkEvent(
            author="user",
            content=genai_types.Content(parts=[genai_types.Part.from_text(text=query)])
        )
        parent_session = Session(
            id=session_id,
            app_name="consult_agent",
            user_id=ctx.auth.get_user_id() or "anonymous_user",
            state={},
            events=[adk_event]
        )
        from google.adk.sessions.in_memory_session_service import InMemorySessionService
        parent_ctx = InvocationContext(
            invocation_id=f"inv_{session_id}",
            branch="0",
            session=parent_session,
            session_service=InMemorySessionService()
        )

        
        subagent_output = ""
        async with httpx.AsyncClient(headers=headers, timeout=90.0) as httpx_client:
            # Instantiate the Remote A2A Agent using the ADK Client
            subagent = RemoteA2aAgent(
                name=valid_name,
                agent_card=card_url,
                httpx_client=httpx_client,
                a2a_request_meta_provider=request_meta_provider
            )
            collected_chunks = []
            last_partial_out = ""
            async for ev in subagent.run_async(parent_context=parent_ctx):
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
                        wtype = parameters.get("widgetType") or parameters.get("widget_type") or parameters.get("id")
                        ctx.actions.append({
                            "type": "OPEN_ADMIN_WIDGET",
                            "payload": {
                                "id": wtype,
                                "widgetType": wtype
                            }
                        })
                        return message or f"Opening the {wtype} widget."
                        
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


@hubscape_adk.require_tool_privilege
async def consultAgentsParallel(calls: list[dict]) -> str:
    """
    Consults multiple specialized subagents concurrently in parallel (max 3 subagents).
    
    Args:
        calls: A list of dicts specifying subagents to consult.
               Example: [{"agentId": "admin_ui_agent", "query": "open edit prompt widget"}, {"agentId": "knowledge_agent", "query": "prompt guidelines"}]
    """
    try:
        import asyncio
        if not calls or not isinstance(calls, list):
            return json.dumps({"error": "calls parameter must be a non-empty list of tool call dictionaries."})

        # Programmatically enforce hard safety cap of Top-3 subagents max
        capped_calls = calls[:3]

        async def run_single_call(c: dict) -> tuple[str, str]:
            aid = c.get("agentId") or c.get("agent_id") or ""
            q = c.get("query") or ""
            if not aid:
                return "unknown", "Error: Missing agentId in parallel call payload."
            res = await consultAgent(agentId=aid, query=q)
            return aid, res

        tasks = [run_single_call(c) for c in capped_calls]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        combined_results = {}
        for item in results_list:
            if isinstance(item, Exception):
                logger.error(f"Error in consultAgentsParallel item: {item}")
                continue
            aid, output = item
            combined_results[aid] = output

        return json.dumps(combined_results, indent=2)

    except Exception as e:
        logger.error(f"Error in consultAgentsParallel: {e}", exc_info=True)
        return json.dumps({"error": f"Failed to execute parallel subagent consultation: {str(e)}"})
