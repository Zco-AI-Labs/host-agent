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

allow_web_search = False
allow_google_maps = False

config_json_path = os.path.join(runtime_dir, "config.json")
if not os.path.exists(config_json_path):
    config_json_path = os.path.join(os.path.dirname(runtime_dir), "config.json")
if os.path.exists(config_json_path):
    try:
        import json
        with open(config_json_path, "r", encoding="utf-8") as cf:
            config_data = json.load(cf)
            if "allow_web_search" in config_data or "allowWebSearch" in config_data:
                allow_web_search = bool(config_data.get("allow_web_search") if "allow_web_search" in config_data else config_data.get("allowWebSearch"))
            if "allow_google_maps" in config_data or "allowGoogleMaps" in config_data:
                allow_google_maps = bool(config_data.get("allow_google_maps") if "allow_google_maps" in config_data else config_data.get("allowGoogleMaps"))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to read/parse config.json: {e}")

grounding_tools = []
if allow_web_search:
    try:
        from google.adk.tools import google_search
        grounding_tools.append(google_search)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to import google_search tool: {e}")

if allow_google_maps:
    try:
        from google.adk.tools import google_maps_grounding
        grounding_tools.append(google_maps_grounding)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to import google_maps_grounding tool: {e}")

from app.app_utils.vertex_gemini import get_model

root_agent = AdkAgent(
    model=get_model("gemini-2.5-flash"),
    name=agent_name,
    description=agent_description,
    instruction=base_skill_instruction,
    tools=tools
)

grounding_agent = None
if grounding_tools:
    grounding_agent = AdkAgent(
        model=get_model("gemini-2.5-flash"),
        name=f"{agent_name}_grounding",
        description="Grounding agent for web search and maps",
        instruction=base_skill_instruction,
        tools=grounding_tools
    )



class HostAgent:
    def __init__(self):
        self.runner = None
        self.grounding_runner = None

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
        import uuid as uuid_mod
        session_id = (context or {}).get("sessionId") or f"sess_{uuid_mod.uuid4().hex[:12]}"

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
            base_instruction = f"[IDENTITY & PERSONA]\n{dynamic_ctx_prompt}\n\n[CORE ORCHESTRATION & MEMORY DIRECTIVES]\n{base_skill_instruction}"
        else:
            base_instruction = base_skill_instruction

        spatial_lines = []
        user_loc = (context or {}).get("user_location") or (context or {}).get("userLocation")
        if user_loc:
            if isinstance(user_loc, dict):
                lat = user_loc.get("latitude") or user_loc.get("lat")
                lng = user_loc.get("longitude") or user_loc.get("lng")
                lbl = user_loc.get("label") or user_loc.get("address") or user_loc.get("city") or ""
                if lbl and not (str(lat) in str(lbl) and str(lng) in str(lbl)):
                    loc_str = f"{lbl} (Latitude: {lat}, Longitude: {lng})"
                elif lat and lng:
                    loc_str = f"Latitude {lat}, Longitude {lng}"
                else:
                    loc_str = str(lbl or user_loc)
                spatial_lines.append(f"📍 User Live Location: {loc_str}")
            elif isinstance(user_loc, str):
                spatial_lines.append(f"📍 User Live Location: {user_loc}")
        
        hub_loc = (context or {}).get("hub_location") or (context or {}).get("hubLocation") or (context or {}).get("workspace_location")
        if hub_loc:
            if isinstance(hub_loc, dict):
                lat = hub_loc.get("latitude") or hub_loc.get("lat")
                lng = hub_loc.get("longitude") or hub_loc.get("lng")
                lbl = hub_loc.get("label") or hub_loc.get("address") or hub_loc.get("name") or ""
                if lbl and lat and lng:
                    loc_str = f"{lbl} (Latitude: {lat}, Longitude: {lng})"
                elif lat and lng:
                    loc_str = f"Latitude {lat}, Longitude {lng}"
                else:
                    loc_str = str(lbl or hub_loc)
                spatial_lines.append(f"🏢 Active Workspace Location: {loc_str}")
            elif isinstance(hub_loc, str):
                spatial_lines.append(f"🏢 Active Workspace Location: {hub_loc}")

        if spatial_lines:
            spatial_context = "\n[SPATIAL & LOCATION CONTEXT]\n" + "\n".join(spatial_lines) + "\n"
            base_instruction = f"{spatial_context}\n{base_instruction}"

        # Determine if query should route to grounding_agent (to prevent Vertex AI tool mixing collision)
        use_grounding = grounding_agent is not None and (
            bool(spatial_lines) or any(kw in parsed_question.lower() for kw in ("distance", "far", "direction", "map", "drive", "navigate", "search", "where", "route", "how long"))
        )

        active_agent = grounding_agent if use_grounding else root_agent
        if use_grounding:
            grounding_override = (
                "\n\n[LIVE GROUNDING & NAVIGATION DIRECTIVE]\n"
                "You are explicitly authorized to use your Google Maps tool to provide real-time directions, "
                "driving/transit distances, travel times, and local routing relative to the user's live location "
                "and workspace location. Do not refuse distance or mapping queries."
            )
            active_agent.instruction = f"{base_instruction}{grounding_override}"
        else:
            active_agent.instruction = base_instruction

        with hubscape_adk.context_session(remote_ctx):
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
            except Exception as mem_err:
                from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
                memory_service = InMemoryMemoryService()

            if use_grounding:
                if not self.grounding_runner:
                    self.grounding_runner = Runner(
                        agent=grounding_agent,
                        app_name='host-agent-grounding',
                        session_service=InMemorySessionService(),
                        artifact_service=InMemoryArtifactService(),
                        memory_service=memory_service,
                        credential_service=InMemoryCredentialService(),
                        auto_create_session=True
                    )
                active_runner = self.grounding_runner
            else:
                if not self.runner:
                    self.runner = Runner(
                        agent=root_agent,
                        app_name='host-agent',
                        session_service=InMemorySessionService(),
                        artifact_service=InMemoryArtifactService(),
                        memory_service=memory_service,
                        credential_service=InMemoryCredentialService(),
                        auto_create_session=True
                    )
                active_runner = self.runner
            
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
                    res = await memory_service.search_memory(
                        app_name='host-agent',
                        user_id=memory_user_id,
                        query=parsed_question
                    )
                    memories = getattr(res, "memories", res) or []
                    memory_lines = []
                    for m in memories:
                        content_val = getattr(m, "content", None)
                        if content_val:
                            if hasattr(content_val, "parts"):
                                parts_text = " ".join([p.text for p in content_val.parts if hasattr(p, "text") and p.text])
                                if parts_text.strip():
                                    memory_lines.append(f"- {parts_text.strip()}")
                            elif isinstance(content_val, str) and content_val.strip():
                                memory_lines.append(f"- {content_val.strip()}")

                    if memory_lines:
                        memory_text = "\n".join(memory_lines)
                        root_agent.instruction = f"{base_instruction}\n\n[USER LONG-TERM MEMORIES & PREFERENCES]\n{memory_text}\n"
                        print(f"🧠 Injected {len(memory_lines)} retrieved user memories into turn context (scope={memory_user_id})")
                except Exception as mem_search_err:
                    print(f"⚠️ Memory search non-critical: {mem_search_err}")

            turn_prompt = parsed_question
            turn_prefix = ""
            if spatial_lines:
                turn_prefix += "\n[SPATIAL & LOCATION CONTEXT]\n" + "\n".join(spatial_lines) + "\n"
            if use_grounding:
                turn_prefix += (
                    "[LIVE GROUNDING & NAVIGATION DIRECTIVE]\n"
                    "You are explicitly authorized to use your Google Maps tool to provide real-time directions, "
                    "driving/transit distances, travel times, and local routing relative to the user's live location "
                    "and workspace location. Do not refuse distance or mapping queries.\n\n"
                )
            if turn_prefix:
                turn_prompt = f"{turn_prefix}{parsed_question}"

            new_message = types.Content(
                parts=[types.Part.from_text(text=turn_prompt)]
            )
            
            collected_outputs = []
            last_partial_output = ""
            async for event in active_runner.run_async(
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
                    if getattr(event, "partial", False):
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
            
            # 3. Retrieve updated session state, ingest to Memory Bank, and persist back to Firestore
            try:
                session_service = active_runner.session_service
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

                # Instant Direct Memory Bank Ingestion
                if memory_service and user_id and user_id != "anonymous_user":
                    try:
                        from google.adk.memory.base_memory_service import MemoryEntry
                        turn_summary = f"User: {parsed_question}\nHost: {text_response}"
                        content_obj = types.Content(parts=[types.Part.from_text(text=turn_summary)])
                        mem_entry = MemoryEntry(content=content_obj)
                        await memory_service.add_memory(app_name='host-agent', user_id=memory_user_id, memories=[mem_entry])
                        print(f"🧠 Instant direct memory ingested to VertexAiMemoryBankService (scope={memory_user_id})")
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

