import os
import uuid
import json
import time
from google.genai import types
from google.adk.runners import Runner
from app.core import hubscape_adk

class GEAPAgentWrapper:
    def __init__(self, agent, app_name: str = None):
        self.agent = agent
        self.app_name = app_name or agent.name.replace('_', '-')
        self.runner = None

    async def query(self, question: str, context: dict = None) -> str:
        start_time = time.time()
        core_dir = os.path.dirname(os.path.abspath(__file__))
        runtime_dir = os.path.abspath(os.path.join(core_dir, ".."))

        user_id = (context or {}).get("userId") or (context or {}).get("user_id") or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/Zco-AI-Labs/{self.app_name}"))
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
        
        session_id = (context or {}).get("sessionId") or (context or {}).get("session_id") or f"session_{user_id}_{hub_id}"
        
        # --- OPENTELEMETRY CONTEXT ENRICHMENT ---
        try:
            from opentelemetry import trace
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("org_id", org_id or "unknown")
                current_span.set_attribute("hub_id", hub_id or "unknown")
                current_span.set_attribute("user_id", user_id or "unknown")
                current_span.set_attribute("gen_ai.conversation_id", session_id)
                current_span.set_attribute("gen_ai.request.model", self.agent.model.model_name)
                current_span.set_attribute("provider", "vertex")
                
                depth = (context or {}).get("depth", 0)
                request_type = "a2a" if depth > 0 else "direct"
                current_span.set_attribute("gen_ai.request.type", request_type)
        except Exception:
            pass
        
        with hubscape_adk.context_session(remote_ctx):
            from google.adk.sessions.in_memory_session_service import InMemorySessionService
            from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
            from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
            from google.adk.auth.credential_service.in_memory_credential_service import InMemoryCredentialService
            
            session_service = InMemorySessionService()
            artifact_service = InMemoryArtifactService()
            memory_service = InMemoryMemoryService()
            credential_service = InMemoryCredentialService()

            # 1. Restore ADK session trajectory from Firestore if available
            try:
                session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id)
                if session_doc and "adk_session" in session_doc:
                    adk_session_json = session_doc["adk_session"]
                    from google.adk.sessions import Session
                    session_obj = Session.model_validate_json(adk_session_json)
                    
                    app_name = session_obj.app_name
                    uid = session_obj.user_id
                    sid = session_obj.id
                    
                    if app_name not in session_service.sessions:
                        session_service.sessions[app_name] = {}
                    if uid not in session_service.sessions[app_name]:
                        session_service.sessions[app_name][uid] = {}
                    session_service.sessions[app_name][uid][sid] = session_obj
            except Exception as restore_err:
                print(f"⚠️ Non-critical: Failed to restore session trajectory: {restore_err}")

            workspace_type = (context or {}).get("workspaceType")
            workspace_id = (context or {}).get("workspaceId")
            if not workspace_type or not workspace_id:
                is_org_scope = (hub_id == org_id) or (not hub_id) or (hub_id == "platform")
                workspace_type = "organization" if is_org_scope else "hub"
                workspace_id = org_id if is_org_scope else hub_id

            # Concurrency-safe dynamic tool filtering based on workspace scope and user privileges
            cloned_agent = hubscape_adk.filter_tools_for_scope(
                agent=self.agent,
                user_privileges=remote_ctx.user_privileges,
                workspace_type=workspace_type,
                workspace_id=workspace_id,
                org_id=org_id
            )
            
            # Resolve remote MCP headers and access control whitelists asynchronously
            cloned_agent = await hubscape_adk.resolve_mcp_tools(cloned_agent, remote_ctx)
            
            raw_mode = (context or {}).get("interaction_mode") or (context or {}).get("mode") or "chat_pc"
            normalized_mode = "chat_pc" if raw_mode == "chat_phone" else raw_mode
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

            spatial_context = ""
            if spatial_lines:
                spatial_context = "\n[SPATIAL & LOCATION CONTEXT]\n" + "\n".join(spatial_lines) + "\n"

            session_context = (
                f"[ACTIVE WORKSPACE CONTEXT]\n"
                f"- Interaction Mode: {normalized_mode}\n"
                f"- Workspace Type: {workspace_type}\n"
                f"- Workspace ID: {workspace_id or 'none'}\n"
                f"- Organization ID: {org_id or 'none'}\n"
                f"{spatial_context}"
            )
            base_instruction = self.agent.instruction or ""
            cloned_agent.instruction = f"{session_context}\n{base_instruction}"
            
            # Create a fresh runner for this request to guarantee thread safety
            runner = Runner(
                agent=cloned_agent,
                app_name=self.app_name,
                session_service=session_service,
                artifact_service=artifact_service,
                memory_service=memory_service,
                credential_service=credential_service,
                auto_create_session=True
            )
            
            turn_prompt = question
            turn_prefix = ""
            if spatial_context:
                turn_prefix += f"{spatial_context.strip()}\n"
            if use_grounding:
                turn_prefix += (
                    "[LIVE GROUNDING & NAVIGATION DIRECTIVE]\n"
                    "You are explicitly authorized to use your Google Maps tool to provide real-time directions, "
                    "driving/transit distances, travel times, and local routing relative to the user's live location "
                    "and workspace location. Do not refuse distance or mapping queries.\n\n"
                )
            if turn_prefix:
                turn_prompt = f"{turn_prefix}{question}"

            new_message = types.Content(
                parts=[types.Part.from_text(text=turn_prompt)]
            )
            
            # Ensure active session object is created and linked to remote_ctx on Turn 1:
            session_obj = await runner.session_service.get_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id
            )
            if not session_obj:
                session_obj = await runner.session_service.create_session(
                    app_name=self.app_name,
                    user_id=user_id,
                    session_id=session_id
                )
            remote_ctx.session = session_obj  # Now ctx.session exists on ALL turns!
            
            collected_outputs = []
            async for event in runner.run_async(
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
            
            # 2. Persist updated ADK session state back to Firestore
            try:
                updated_session = await runner.session_service.get_session(
                    app_name=self.app_name,
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
            except Exception as save_err:
                print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")

            # Record final execution latency on active span
            try:
                from opentelemetry import trace
                current_span = trace.get_current_span()
                if current_span:
                    latency_ms = (time.time() - start_time) * 1000.0
                    current_span.set_attribute("latency_ms", float(latency_ms))
            except Exception:
                pass
                
            return text_response

