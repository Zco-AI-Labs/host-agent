import logging
import asyncio
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

async def run_agent_parallel(requests: list) -> dict:
    """
    Executes queries to multiple specialized subagents in parallel.
    
    Args:
        requests: A list of dictionaries, where each dict has:
            - "agentId": The ID of the target subagent.
            - "query": The instruction/query for that subagent.
            
    Returns:
        A dictionary mapping agent IDs to their respective text outputs or error messages.
    """
    try:
        ctx = hubscape_adk.get_context()
        raw_ctx = ctx.raw_context
        
        # Prevent infinite agent-to-agent delegation loops (max depth = 3)
        current_depth = raw_ctx.get("depth", 0)
        max_depth = 3
        if current_depth >= max_depth:
            return {"error": f"Maximum agent delegation depth of {max_depth} exceeded. Aborting parallel execution."}
            
        accessible_agents = raw_ctx.get("accessible_agents", [])
        
        # 1. Resolve subagents whitelists and A2A urls
        def normalize(s: str) -> str:
            return "".join(c for c in s.lower() if c.isalnum())
            
        def find_agent(agent_id: str):
            normalized_query_id = normalize(agent_id)
            for agent in accessible_agents:
                aid = agent.get("id") or ""
                aname = agent.get("name") or ""
                if aid == agent_id or normalize(aid) == normalized_query_id or normalize(aname) == normalized_query_id:
                    return agent
            return None

        # 2. Get GCP access token
        credentials, project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        token = credentials.token
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Create shared HTTP client
        httpx_client = httpx.AsyncClient(headers=headers, timeout=90.0)
        
        async def execute_one(req: dict) -> tuple:
            agent_id = req.get("agentId")
            query = req.get("query")
            if not agent_id or not query:
                return agent_id or "unknown", "Error: Missing agentId or query in request."
                
            target_agent = find_agent(agent_id)
            if not target_agent:
                return agent_id, f"Error: Agent '{agent_id}' is not accessible or not whitelisted."
                
            # Resolve A2A URL
            a2a_url = target_agent.get("a2aUrl")
            resource_name = target_agent.get("geap_resource_name")
            if not a2a_url and resource_name:
                location = "us-central1"
                if "/" in resource_name:
                    parts = resource_name.split("/")
                    if len(parts) > 3:
                        location = parts[3]
                a2a_url = f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}"
                
            if not a2a_url:
                return agent_id, f"Error: Agent '{agent_id}' does not have a valid A2A URL."
                
            try:
                # Request metadata provider to securely propagate RBAC context and increment call depth
                def request_meta_provider(invocation_context, a2a_message):
                    return {
                        "userId": ctx.auth.get_user_id(),
                        "user_id": ctx.auth.get_user_id(),
                        "orgId": ctx.auth.org_id,
                        "org_id": ctx.auth.org_id,
                        "hubId": ctx.auth.hub_id,
                        "hub_id": ctx.auth.hub_id,
                        "accessible_agents": accessible_agents,
                        "depth": current_depth + 1
                    }

                subagent = RemoteA2aAgent(
                    name=agent_id,
                    agent_card=a2a_url,
                    httpx_client=httpx_client,
                    a2a_request_meta_provider=request_meta_provider
                )
                
                adk_event = AdkEvent(
                    author="user",
                    content=genai_types.Content(parts=[genai_types.Part.from_text(text=query)])
                )
                dummy_session = Session(
                    id="dummy_parallel_session",
                    app_name="run_agent_parallel",
                    user_id="dummy_user",
                    state={},
                    events=[adk_event]
                )
                parent_ctx = InvocationContext(
                    invocation_id="dummy_parallel_invocation",
                    branch=0,
                    session=dummy_session
                )
                
                output = ""
                async for ev in subagent.run_async(parent_context=parent_ctx):
                    if ev.output:
                        output += ev.output
                    elif ev.content and ev.content.parts:
                        for part in ev.content.parts:
                            if part.text:
                                output += part.text
                return agent_id, output
            except Exception as e:
                return agent_id, f"Error: {str(e)}"
                
        # Run all requests concurrently
        tasks = [execute_one(req) for req in requests]
        results = await asyncio.gather(*tasks)
        
        # Close HTTP client
        await httpx_client.aclose()
        
        return {agent_id: output for agent_id, output in results}
        
    except Exception as e:
        logger.error(f"Error in run_agent_parallel: {e}", exc_info=True)
        return {"error": f"Parallel execution failed: {str(e)}"}
