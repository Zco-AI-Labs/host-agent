import os
import sys

# Ensure app directory is in sys.path before any local imports run
app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# Force regional Vertex AI routing unconditionally
os.environ.pop("GOOGLE_GENAI_USE_ENTERPRISE", None)
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)
import asyncio
import importlib.util
import re
import json
import time
from google.adk import Agent as AdkAgent
from google.adk.runners import Runner
from google.genai import types

def load_local_tools(scripts_dir: str) -> list:
    import sys
    app_dir = os.path.dirname(os.path.abspath(scripts_dir))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    tools = []
    if not os.path.exists(scripts_dir):
        return tools
    for filename in os.listdir(scripts_dir):
        if filename.endswith(".py") and not filename.startswith("_"):
            module_name = filename[:-3]
            file_path = os.path.join(scripts_dir, filename)
            try:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    func = getattr(module, module_name, None)
                    if not func:
                        # Try camelCase conversion (e.g. consult_agent -> consultAgent)
                        parts = module_name.split("_")
                        camel_name = parts[0] + "".join(p.capitalize() for p in parts[1:])
                        func = getattr(module, camel_name, None)
                    if func and callable(func):
                        tools.append(func)
            except Exception:
                pass
    return tools

# Module-level discovery symbols for ADK CLI
runtime_dir = os.path.dirname(os.path.abspath(__file__))
scripts_dir = os.path.join(runtime_dir, "scripts")

# Statically import scripts to ensure Vertex AI packaging bundles them in the cloud deployment
from app.scripts import (
    consult_agent,
    discover_agents,
    inspect_env,
    run_agent_parallel,
    suggest_queries,
)

tools = load_local_tools(scripts_dir)

from app.app_utils.vertex_gemini import get_model

root_agent = AdkAgent(
    model=get_model("gemini-2.5-flash"),
    name='host_agent',
    description='Managed GEAP Host Orchestrator.',
    instruction="You are the Hubscape central Host agent.",
    tools=tools
)

# Dynamically patch AdkAgent to use a thread-safe, task-local ContextVar for instruction
from contextvars import ContextVar
_active_instruction = ContextVar("_active_instruction", default=None)
_default_instruction = "You are the Hubscape central Host agent."

@property
def dynamic_instruction(self):
    return _active_instruction.get() or _default_instruction

@dynamic_instruction.setter
def dynamic_instruction(self, value):
    _active_instruction.set(value)

AdkAgent.instruction = dynamic_instruction

class HostAgent:
    def __init__(self):
        self.runner = None

    async def query(self, question: str, context: dict = None) -> str:
        start_time = time.time()
        runtime_dir = os.path.dirname(os.path.abspath(__file__))
        
        # --- A2A JSON-RPC WRAPPING PARSER ---
        parsed_question = question
        try:
            payload = json.loads(question)
            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                method = payload.get("method")
                if method in ("message/send", "message.send"):
                    params = payload.get("params") or {}
                    message = params.get("message") or {}
                    parts = message.get("parts") or []
                    text_parts = [p.get("text", "") for p in parts if "text" in p]
                    if text_parts:
                        parsed_question = "\n".join(text_parts)
                elif "params" in payload and isinstance(payload["params"], dict):
                    parsed_question = payload["params"].get("query") or payload["params"].get("message") or question
        except Exception:
            pass
        

        import hubscape_adk
        import uuid
        user_id = (context or {}).get("userId") or (context or {}).get("user_id") or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        # Calculate stable host-agent UUID
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        from app.app_utils.env_resolver import get_project_id
        project_id = get_project_id()
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id, 
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=context
        )
        
        # Resolve session ID
        session_id = (context or {}).get("sessionId") or f"session_{user_id}_{hub_id}"

        # --- OPENTELEMETRY CONTEXT ENRICHMENT (OPTION A) ---
        try:
            from opentelemetry import trace
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("org_id", org_id or "unknown")
                current_span.set_attribute("hub_id", hub_id or "unknown")
                current_span.set_attribute("user_id", user_id or "unknown")
                current_span.set_attribute("gen_ai.conversation_id", session_id)
                current_span.set_attribute("gen_ai.request.model", root_agent.model.model_name)
                current_span.set_attribute("provider", "vertex")
                
                # Determine query type (direct vs nested A2A) using call depth
                depth = (context or {}).get("depth", 0)
                request_type = "a2a" if depth > 0 else "direct"
                current_span.set_attribute("gen_ai.request.type", request_type)
        except Exception as otel_err:
            print(f"⚠️ Failed to set OpenTelemetry span attributes: {otel_err}")
        # ----------------------------------------------------

        # --- FAST-PATH ACTION INTERCEPTOR ---
        if parsed_question.startswith("/action switchHub"):
            parts = parsed_question.split(" ", 2)
            if len(parts) >= 2:
                action_payload = {}
                if len(parts) == 3:
                    try:
                        action_payload = json.loads(parts[2])
                    except Exception:
                        pass
                target_hub = action_payload.get("hubId")
                if target_hub:
                    remote_ctx.actions.append({
                        "type": "SWITCH_HUB",
                        "payload": {
                            "hubId": target_hub
                        }
                    })
                    return json.dumps({
                        "text": f"Switching context to hub: {target_hub}",
                        "actions": remote_ctx.actions
                    })
        
        # 1. Resolve dynamic system instructions from context
        system_instruction = (context or {}).get("system_instruction") or "You are the Hubscape central Host agent."
        root_agent.instruction = system_instruction
        
        with hubscape_adk.context_session(remote_ctx):
            if not self.runner:
                from google.adk.sessions.in_memory_session_service import InMemorySessionService
                from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
                from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
                from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
                
                self.runner = Runner(
                    agent=root_agent,
                    app_name='host-agent',
                    session_service=InMemorySessionService(),
                    artifact_service=InMemoryArtifactService(),
                    memory_service=InMemoryMemoryService(),
                    credential_service=InMemoryCredentialService(),
                    auto_create_session=True
                )
            
            # 2. Try to restore session trajectory from Firestore using ADK serialization
            try:
                session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id)
                if session_doc and "adk_session" in session_doc:
                    adk_session_json = session_doc["adk_session"]
                    from google.adk.sessions import Session
                    session_obj = Session.model_validate_json(adk_session_json)
                    
                    # Inject loaded session into InMemorySessionService cache
                    session_service = self.runner.session_service
                    app_name = session_obj.app_name
                    uid = session_obj.user_id
                    sid = session_obj.id
                    
                    if app_name not in session_service.sessions:
                        session_service.sessions[app_name] = {}
                    if uid not in session_service.sessions[app_name]:
                        session_service.sessions[app_name][uid] = {}
                    session_service.sessions[app_name][uid][sid] = session_obj
                    print(f"🔄 Resumed ADK GEAP Session: {session_id}")
                else:
                    print(f"🌱 Starting New ADK GEAP Session: {session_id}")
            except Exception as restore_err:
                print(f"⚠️ Non-critical: Failed to restore session trajectory: {restore_err}")

            new_message = types.Content(
                parts=[types.Part.from_text(text=parsed_question)]
            )
            
            text_response = ""
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message
            ):
                if event.output:
                    text_response += event.output
                elif event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            text_response += part.text
            
            # 3. Retrieve updated session state and persist back to Firestore
            try:
                session_service = self.runner.session_service
                updated_session = await session_service.get_session(
                    app_name='host-agent',
                    user_id=user_id,
                    session_id=session_id
                )
                if updated_session:
                    serialized_json = updated_session.model_dump_json()
                    remote_ctx.save(
                        scope="user",
                        collection_name="sessions",
                        doc_id=session_id,
                        data={
                            "adk_session": serialized_json
                        }
                    )
                    print(f"💾 Persisted ADK GEAP Session trajectory for {session_id}")
            except Exception as save_err:
                print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")
                
            # Record final execution latency on active span
            try:
                from opentelemetry import trace
                current_span = trace.get_current_span()
                if current_span:
                    latency_ms = (time.time() - start_time) * 1000.0
                    current_span.set_attribute("latency_ms", float(latency_ms))
            except Exception as otel_err:
                pass
                
            # Fetch any actions collected during the context session
            actions = getattr(remote_ctx, "actions", [])
            
            # Return the result as a structured JSON string
            return json.dumps({
                "text": text_response,
                "actions": actions
            })

# Singleton instance used as the serialization target
host_agent_app = HostAgent()

from google.adk.apps import App
app = App(
    root_agent=root_agent,
    name="app",
)

