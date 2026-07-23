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
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to load tool {module_name} from {file_path}: {e}", exc_info=True)
    return tools

# 1. Require SKILL.md as the Single Source of Truth for metadata (name, description) and instructions
runtime_dir = os.path.dirname(os.path.abspath(__file__))
skill_md_path = os.path.join(runtime_dir, "SKILL.md")
if not os.path.exists(skill_md_path):
    raise FileNotFoundError(f"Required agent definition file missing: {skill_md_path}")

with open(skill_md_path, "r", encoding="utf-8") as f:
    skill_content = f.read()

fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", skill_content, flags=re.DOTALL)
if not fm_match:
    raise ValueError(f"SKILL.md is missing required YAML frontmatter header (--- ... ---): {skill_md_path}")

fm_text = fm_match.group(1)
name_m = re.search(r'^name:\s*["\']?([^"\'\n]+)["\']?', fm_text, re.MULTILINE)
if not name_m:
    raise ValueError(f"SKILL.md frontmatter is missing required 'name:' field: {skill_md_path}")

desc_m = re.search(r'^description:\s*["\']?([^"\'\n]+)["\']?', fm_text, re.MULTILINE)
if not desc_m:
    raise ValueError(f"SKILL.md frontmatter is missing required 'description:' field: {skill_md_path}")

agent_name = name_m.group(1).strip().replace('-', '_')
agent_description = desc_m.group(1).strip()
base_skill_instruction = skill_content[fm_match.end():].strip()

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
    name=agent_name,
    description=agent_description,
    instruction=base_skill_instruction,
    tools=tools
)



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
        

        from app.core import hubscape_adk
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
        
        # 1. Resolve dynamic system instructions from context and merge with base skill instructions
        dynamic_ctx_prompt = (context or {}).get("system_instruction") or ""
        if dynamic_ctx_prompt:
            root_agent.instruction = f"[IDENTITY & PERSONA]\n{dynamic_ctx_prompt}\n\n[CORE ORCHESTRATION & MEMORY DIRECTIVES]\n{base_skill_instruction}"
        else:
            root_agent.instruction = base_skill_instruction

        
        with hubscape_adk.context_session(remote_ctx):
            if not self.runner:
                from google.adk.sessions.in_memory_session_service import InMemorySessionService
                from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
                from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
                
                memory_service = None
                try:
                    from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
                    from app.app_utils.env_resolver import get_project_id
                    project_id = get_project_id()
                    location = os.getenv("GCP_LOCATION") or "us-central1"
                    
                    engine_id = None
                    for key in ['REASONING_ENGINE_ID', 'AGENT_ENGINE_ID', 'GEAP_HOST_RESOURCE', 'RESOURCE_NAME']:
                        val = os.getenv(key)
                        if val:
                            if 'reasoningEngines/' in val:
                                engine_id = val.split('reasoningEngines/')[-1].split('/')[0]
                                break
                            if val.isdigit():
                                engine_id = val
                                break
                    if not engine_id:
                        engine_id = "1953980046871887872"
                        
                    memory_service = VertexAiMemoryBankService(project=project_id, location=location, agent_engine_id=engine_id)
                    print(f"🧠 Connected GEAP VertexAiMemoryBankService (project={project_id}, location={location}, engine_id={engine_id}) to host-agent")
                except Exception as mem_err:
                    print(f"ℹ️ VertexAiMemoryBankService fallback ({mem_err}). Using InMemoryMemoryService.")
                    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
                    memory_service = InMemoryMemoryService()

                self.runner = Runner(
                    agent=root_agent,
                    app_name='host-agent',
                    session_service=InMemorySessionService(),
                    artifact_service=InMemoryArtifactService(),
                    memory_service=memory_service,
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

            # Tenant-Isolated Memory User Key (Prevents cross-org data leakage)
            org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
            memory_user_id = f"{org_id}:{user_id}" if org_id else user_id

            # Pre-turn Memory Bank Search
            if memory_service and user_id and user_id != "anonymous_user":
                try:
                    memories = await memory_service.search_memory(
                        app_name='host-agent',
                        user_id=memory_user_id,
                        query=parsed_question
                    )
                    if memories:
                        memory_text = "\n".join([f"- {m.content}" for m in memories if getattr(m, 'content', None)])
                        if memory_text.strip():
                            root_agent.instruction += f"\n\n[USER LONG-TERM MEMORIES & PREFERENCES]\n{memory_text}\n"
                            print(f"🧠 Injected {len(memories)} retrieved user memories into turn context (scope={memory_user_id})")
                except Exception as mem_search_err:
                    print(f"⚠️ Memory search non-critical: {mem_search_err}")

            new_message = types.Content(
                parts=[types.Part.from_text(text=parsed_question)]
            )
            
            collected_outputs = []
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message
            ):
                out = getattr(event, "output", None)
                if not out and getattr(event, "content", None) and getattr(event.content, "parts", None):
                    text_parts = [p.text for p in event.content.parts if getattr(p, "text", None)]
                    if text_parts:
                        out = "\n".join(text_parts)
                if out and isinstance(out, str) and out.strip():
                    clean_out = out.strip()
                    if not collected_outputs or clean_out != collected_outputs[-1].strip():
                        collected_outputs.append(clean_out)
            
            text_response = "\n".join(collected_outputs)
            
            # 3. Retrieve updated session state, ingest to Memory Bank, and persist back to Firestore
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

                    # Post-turn Memory Bank Ingestion
                    if memory_service:
                        try:
                            # Re-key session user_id to tenant-isolated key before memory ingestion
                            session_copy = updated_session.model_copy()
                            session_copy.user_id = memory_user_id
                            await memory_service.add_session_to_memory(session_copy)
                            print(f"🧠 Ingested session turn to VertexAiMemoryBankService (scope={memory_user_id})")
                        except Exception as mem_ingest_err:
                            print(f"⚠️ Memory ingestion non-critical: {mem_ingest_err}")
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

