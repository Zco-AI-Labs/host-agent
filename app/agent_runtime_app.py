# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys
import os
# Ensure standard imports share the same module instance
app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# pyopenssl monkeypatching
try:
    from urllib3.contrib import pyopenssl
    pyopenssl.extract_from_urllib3()
    
    from urllib3.contrib.pyopenssl import PyOpenSSLContext
    def make_safe(func):
        if not func: return func
        def safe_func(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except ValueError as e:
                if "cannot be mutated again" in str(e): return None
                raise
        return safe_func
        
    for prop_name in ["verify_mode", "verify_flags", "options", "minimum_version", "maximum_version"]:
        prop = getattr(PyOpenSSLContext, prop_name, None)
        if prop and prop.fset:
            setattr(PyOpenSSLContext, prop_name, property(prop.fget, make_safe(prop.fset), prop.fdel))
    for method_name in ["load_cert_chain", "load_verify_locations", "set_ciphers", "set_alpn_protocols", "set_default_verify_paths"]:
        method = getattr(PyOpenSSLContext, method_name, None)
        if method:
            setattr(PyOpenSSLContext, method_name, make_safe(method))
except Exception:
    pass

import asyncio
import logging
import concurrent.futures
from typing import Any, Optional, Dict, List, Union

import nest_asyncio
import vertexai
from dotenv import load_dotenv
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, TransportProtocol
from a2a.server.agent_execution import RequestContext
from a2a.server.events.event_queue import EventQueue

from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.apps import App
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.cloud import logging as google_cloud_logging
from vertexai.preview.reasoning_engines import A2aAgent

from app.core import hubscape_adk
from contextvars import ContextVar
telemetry_org_id = ContextVar("telemetry_org_id", default=None)
telemetry_hub_id = ContextVar("telemetry_hub_id", default=None)
telemetry_user_id = ContextVar("telemetry_user_id", default=None)
telemetry_conversation_id = ContextVar("telemetry_conversation_id", default=None)
request_runner_ctx = ContextVar("request_runner_ctx", default=None)

from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry import trace

class BillingContextLogRecordProcessor(LogRecordProcessor):
    def emit(self, log_record, context=None):
        self.on_emit(log_record, context)

    def on_emit(self, log_record, context=None):
        try:
            # 1. Try tracing span attributes
            span = trace.get_current_span()
            if span and span.get_span_context().is_valid:
                span_attribs = getattr(span, "attributes", None)
                if span_attribs:
                    for key in ["org_id", "hub_id", "user_id", "gen_ai.conversation_id", "gen_ai.request.model", "gen_ai_request_model", "provider", "latency_ms"]:
                        if key in span_attribs:
                            val = span_attribs[key]
                            log_record_inner = getattr(log_record, "log_record", None)
                            if log_record_inner:
                                if not log_record_inner.attributes:
                                    log_record_inner.attributes = {}
                                log_record_inner.attributes[key] = val
                            else:
                                if not log_record.attributes:
                                    log_record.attributes = {}
                                log_record.attributes[key] = val

            # 2. Fallback/overwrite with task-local ContextVars (extremely reliable)
            ctx_vars = {
                "org_id": telemetry_org_id.get(),
                "hub_id": telemetry_hub_id.get(),
                "user_id": telemetry_user_id.get()
            }
            for key, val in ctx_vars.items():
                if val is not None:
                    log_record_inner = getattr(log_record, "log_record", None)
                    if log_record_inner:
                        if not log_record_inner.attributes:
                            log_record_inner.attributes = {}
                        log_record_inner.attributes[key] = val
                    else:
                        if not log_record.attributes:
                            log_record.attributes = {}
                        log_record.attributes[key] = val
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass

from app.agent import app as adk_app
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

load_dotenv()

import os
def _load_privileges() -> dict:
    import json
    privileges_data = {}
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        privileges_path = os.path.join(app_dir, "privileges.json")
        if os.path.exists(privileges_path):
            with open(privileges_path, "r") as pf:
                privileges_data = json.load(pf)
    except Exception:
        pass
    return privileges_data

def _load_privileges_without_tools() -> dict:
    privileges_data = _load_privileges()
    if not privileges_data:
        return {}
    filtered_data = {}
    if "privileges" in privileges_data:
        filtered_data["privileges"] = {}
        for role_id, role_info in privileges_data["privileges"].items():
            if isinstance(role_info, dict):
                filtered_data["privileges"][role_id] = {
                    k: v for k, v in role_info.items() if k != "tools"
                }
            else:
                filtered_data["privileges"][role_id] = role_info
    else:
        filtered_data = privileges_data
    return filtered_data

MAIN_LOOP = None

class ActionInterceptingEventQueue(EventQueue):
    def __init__(self, target_queue: EventQueue, remote_context):
        super().__init__()
        self.target_queue = target_queue
        self.remote_context = remote_context
        self.accumulated_text = ""
        self.events = []
        self.final_event = None
        self.artifact_event = None

    async def enqueue_event(self, event):
        from a2a.types import TaskStatusUpdateEvent, TaskArtifactUpdateEvent
        if isinstance(event, TaskStatusUpdateEvent):
            if event.final:
                self.final_event = event
                return
            
            # Extract text to accumulate
            if event.status and event.status.message and event.status.message.parts:
                for part in event.status.message.parts:
                    if hasattr(part, "text") and part.text:
                        self.accumulated_text += part.text
            
            self.events.append(event)
        elif isinstance(event, TaskArtifactUpdateEvent):
            self.artifact_event = event
        else:
            await self.target_queue.enqueue_event(event)

class AgentEngineA2aExecutor(A2aAgentExecutor):
    """Custom A2A Executor that intercepts requests to inject RemoteContext and handle sessions."""
    async def _resolve_runner(self) -> Runner:
        scoped = request_runner_ctx.get()
        if scoped is not None:
            return scoped
        return await super()._resolve_runner()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ):
        import json
        import uuid
        from datetime import datetime, timezone
        
        metadata = context.metadata or {}
        
        user_id_resolved = metadata.get("userId") or metadata.get("user_id") or "anonymous_user"
        org_id = metadata.get("orgId") or metadata.get("org_id")
        hub_id = metadata.get("hubId") or metadata.get("hub_id")
        mode = metadata.get("mode") or "none"
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        from app.app_utils.env_resolver import get_project_id
        project_id = get_project_id()
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id_resolved,
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=metadata
        )
        
        # --- FAST-PATH ACTION INTERCEPTOR ---
        message = context.get_user_input()
        if message and message.startswith("/action switchHub"):
            parts = message.split(" ", 2)
            if len(parts) >= 2:
                action_payload = {}
                if len(parts) == 3:
                    try:
                        action_payload = json.loads(parts[2])
                    except Exception:
                        pass
                target_hub = action_payload.get("hubId")
                if target_hub:
                    from a2a.types import TaskStatusUpdateEvent, Message, Role, TextPart, TaskStatus, TaskState, TaskArtifactUpdateEvent, Artifact
                    
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "switchHub",
                        "parameters": {
                            "hubId": target_hub
                        },
                        "message": f"Switching context to hub: {target_hub}"
                    }
                    json_text = json.dumps(directive_payload)
                    
                    new_event = TaskStatusUpdateEvent(
                        task_id=context.task_id,
                        status=TaskStatus(
                            state=TaskState.working,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            message=Message(
                                message_id=str(uuid.uuid4()),
                                role=Role.agent,
                                parts=[TextPart(text=json_text)]
                            )
                        ),
                        context_id=context.context_id,
                        final=False
                    )
                    await event_queue.enqueue_event(new_event)

                    new_artifact_event = TaskArtifactUpdateEvent(
                        task_id=context.task_id,
                        last_chunk=True,
                        context_id=context.context_id,
                        artifact=Artifact(
                            artifact_id=str(uuid.uuid4()),
                            parts=[TextPart(text=json_text)]
                        )
                    )
                    await event_queue.enqueue_event(new_artifact_event)
                    return

        interceptor = ActionInterceptingEventQueue(event_queue, remote_ctx)
        
        # Resolve the runner and clone the agent to ensure request-scoped concurrency safety
        base_runner = await super()._resolve_runner()
        cloned_agent = base_runner.agent.clone()
        
        base_instruction = base_runner.agent.instruction or ""
        
        # Check for system_instruction from context/metadata
        system_instruction = metadata.get("system_instruction")
        if system_instruction:
            cloned_agent.instruction = system_instruction
            
        # Instantiate a request-scoped runner to avoid polluting the process-wide singleton
        scoped_runner = Runner(
            agent=cloned_agent,
            app_name=base_runner.app_name,
            session_service=base_runner.session_service,
            artifact_service=getattr(base_runner, "artifact_service", None),
            memory_service=getattr(base_runner, "memory_service", None),
            credential_service=getattr(base_runner, "credential_service", None),
            auto_create_session=getattr(base_runner, "auto_create_session", False),
        )
        
        token = request_runner_ctx.set(scoped_runner)
            
        session_id_resolved = metadata.get("sessionId") or f"session_{user_id_resolved}_{hub_id}"
        
        # --- TELEMETRY CONTEXT VARIABLES SETTING ---
        telemetry_org_id.set(org_id or "unknown")
        telemetry_hub_id.set(hub_id or "unknown")
        telemetry_user_id.set(user_id_resolved or "unknown")
        telemetry_conversation_id.set(session_id_resolved)

        # --- OPENTELEMETRY CONTEXT ENRICHMENT ---
        try:
            from opentelemetry import trace
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("org_id", org_id or "unknown")
                current_span.set_attribute("hub_id", hub_id or "unknown")
                current_span.set_attribute("user_id", user_id_resolved or "unknown")
                current_span.set_attribute("gen_ai.conversation_id", session_id_resolved)
                
                # Determine query type (direct vs nested A2A) using call depth
                depth = metadata.get("depth", 0)
                request_type = "a2a" if depth > 0 else "direct"
                current_span.set_attribute("gen_ai.request.type", request_type)
        except Exception as otel_err:
            print(f"⚠️ Failed to set OpenTelemetry span attributes in executor: {otel_err}")


        
        # 1. Restore ADK Session from Firestore
        try:
            session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id_resolved)
            if session_doc and "adk_session" in session_doc:
                adk_session_json = session_doc["adk_session"]
                from google.adk.sessions import Session
                session_obj = Session.model_validate_json(adk_session_json)
                
                runner = await self._resolve_runner()
                session_service = runner.session_service
                app_name = adk_app.name
                uid = session_obj.user_id
                sid = session_obj.id
                
                if app_name not in session_service.sessions:
                    session_service.sessions[app_name] = {}
                if uid not in session_service.sessions[app_name]:
                    session_service.sessions[app_name][uid] = {}
                session_service.sessions[app_name][uid][sid] = session_obj
                print(f"🔄 Resumed ADK GEAP Session: {session_id_resolved}")
            else:
                print(f"🌱 Starting New ADK GEAP Session: {session_id_resolved}")
        except Exception as restore_err:
            print(f"⚠️ Failed to restore session trajectory: {restore_err}")
            
        try:
            with hubscape_adk.context_session(remote_ctx):
                await super().execute(context, interceptor)
        finally:
            request_runner_ctx.reset(token)

        # 2. Persist ADK Session back to Firestore
        try:
            runner = await self._resolve_runner()
            session_service = runner.session_service
            updated_session = await session_service.get_session(
                app_name=adk_app.name,
                user_id=user_id_resolved,
                session_id=session_id_resolved
            )
            if updated_session:
                serialized_json = updated_session.model_dump_json()
                remote_ctx.save(
                    scope="user",
                    collection_name="sessions",
                    doc_id=session_id_resolved,
                    data={
                        "adk_session": serialized_json
                    }
                )
                print(f"💾 Persisted ADK GEAP Session trajectory for {session_id_resolved}")
        except Exception as save_err:
            print(f"⚠️ Failed to save session trajectory: {save_err}")

        # 3. Propagate Custom Actions
        has_actions = bool(remote_ctx.actions)
        if has_actions:
            directive_payload = {}
            for action in remote_ctx.actions:
                atype = action.get("type")
                payload = action.get("payload") or {}
                if atype == "OPEN_AGENT_WIDGET":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "openAgentWidget",
                        "parameters": {
                            "widgetId": payload.get("widgetId"),
                            "widgetConfig": payload.get("widgetConfig"),
                            "data": payload.get("data") or {},
                            "styling": payload.get("styling") or {},
                            "userPreferences": payload.get("userPreferences") or {}
                        },
                        "message": interceptor.accumulated_text or "Displaying agent widget."
                    }
                    break
                elif atype == "OPEN_ADMIN_WIDGET":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "openAdminWidget",
                        "parameters": {
                            "widgetType": payload.get("widgetType")
                        },
                        "message": interceptor.accumulated_text or "Opening admin widget."
                    }
                    break
                elif atype == "SET_SUGGESTIONS":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "suggestQueries",
                        "parameters": {
                            "queries": action.get("queries") or []
                        },
                        "message": interceptor.accumulated_text or ""
                    }
                    break
                elif atype == "SWITCH_HUB":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "switchHub",
                        "parameters": {
                            "hubId": payload.get("hubId")
                        },
                        "message": interceptor.accumulated_text or "Switching hub workspace."
                    }
                    break
                elif atype == "OPEN_EXTERNAL_LINK":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "openExternalLink",
                        "parameters": {
                            "url": payload.get("url")
                        },
                        "message": interceptor.accumulated_text or "Opening link."
                    }
                    break
                elif atype == "END_CALL":
                    directive_payload = {
                        "directive": "execute_host_tool",
                        "target_tool": "endCall",
                        "parameters": {},
                        "message": interceptor.accumulated_text or "Call ended."
                    }
                    break

            if directive_payload:
                from a2a.types import TaskStatusUpdateEvent, Message, Role, TextPart, TaskStatus, TaskState, TaskArtifactUpdateEvent, Artifact
                
                json_text = json.dumps(directive_payload)
                new_event = TaskStatusUpdateEvent(
                    task_id=context.task_id,
                    status=TaskStatus(
                        state=TaskState.working,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        message=Message(
                            message_id=str(uuid.uuid4()),
                            role=Role.agent,
                            parts=[TextPart(text=json_text)]
                        )
                    ),
                    context_id=context.context_id,
                    final=False
                )
                await event_queue.enqueue_event(new_event)

                new_artifact_event = TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    last_chunk=True,
                    context_id=context.context_id,
                    artifact=Artifact(
                        artifact_id=str(uuid.uuid4()),
                        parts=[TextPart(text=json_text)]
                    )
                )
                await event_queue.enqueue_event(new_artifact_event)
            else:
                for ev in interceptor.events:
                    await event_queue.enqueue_event(ev)
                if interceptor.artifact_event:
                    await event_queue.enqueue_event(interceptor.artifact_event)
        else:
            for ev in interceptor.events:
                await event_queue.enqueue_event(ev)
            if interceptor.artifact_event:
                await event_queue.enqueue_event(interceptor.artifact_event)

        if interceptor.final_event:
            await event_queue.enqueue_event(interceptor.final_event)

class AgentEngineApp(A2aAgent):
    @staticmethod
    def create(
        app: App | None = None,
        artifact_service: Any = None,
        session_service: Any = None,
    ) -> Any:
        if app is None:
            app = adk_app

        def create_runner() -> Runner:
            return Runner(
                app=app,
                session_service=session_service,
                artifact_service=artifact_service,
                auto_create_session=True,
            )

        global MAIN_LOOP
        try:
            MAIN_LOOP = asyncio.get_running_loop()
            nest_asyncio.apply()
        except RuntimeError:
            pass

        agent_card = asyncio.run(AgentEngineApp.build_agent_card(app=app))

        return AgentEngineApp(
            agent_executor_builder=lambda: AgentEngineA2aExecutor(runner=create_runner()),
            agent_card=agent_card,
        )

    @staticmethod
    async def build_agent_card(app: App) -> AgentCard:
        agent_name = app.root_agent.name.replace('_', '-') if app.root_agent and hasattr(app.root_agent, "name") else "custom-agent"
        extensions = [
            AgentExtension(
                uri="https://google.github.io/adk-docs/a2a/a2a-extension/",
                description="Ability to use the new agent executor implementation",
            ),
        ]
        privileges_data = _load_privileges_without_tools()
        if privileges_data:
            extensions.append(
                AgentExtension(
                    uri="https://hubscape.io/extensions/privileges",
                    description="Workspace role-based privileges matrix",
                    params=privileges_data
                )
            )

        agent_card_builder = AgentCardBuilder(
            agent=app.root_agent,
            capabilities=AgentCapabilities(
                streaming=True,
                extensions=extensions,
            ),
            rpc_url="http://localhost:9999/",
            agent_version=os.getenv("AGENT_VERSION", "0.1.0"),
        )
        agent_card = await agent_card_builder.build()
        agent_card.name = agent_name
        agent_card.preferred_transport = TransportProtocol.http_json  # Http Only.
        agent_card.supports_authenticated_extended_card = True
        return agent_card

    def set_up(self) -> None:
        """Initialize the agent engine app with logging and telemetry."""
        try:
            from urllib3.contrib import pyopenssl
            pyopenssl.extract_from_urllib3()
        except Exception:
            pass
        os.environ.pop("GOOGLE_GENAI_USE_ENTERPRISE", None)
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
        if gemini_location:
            os.environ["GOOGLE_CLOUD_LOCATION"] = gemini_location
        vertexai.init()
        setup_telemetry()
        super().set_up()
        
        # Register billing context processor to propagate span attributes to log records
        try:
            from opentelemetry._logs import get_logger_provider
            from opentelemetry.sdk._logs import LoggerProvider
            provider = get_logger_provider()
            if isinstance(provider, LoggerProvider):
                provider.add_log_record_processor(BillingContextLogRecordProcessor())
                logging.info("BillingContextLogRecordProcessor registered successfully on LoggerProvider")
            else:
                logging.warning("LoggerProvider is not a LoggerProvider: %s", type(provider))
        except Exception as otel_reg_err:
            logging.warning("Failed to register BillingContextLogRecordProcessor: %s", otel_reg_err)

        logging.basicConfig(level=logging.INFO)
        logging_client = google_cloud_logging.Client()
        self.logger = logging_client.logger(__name__)


    def register_feedback(self, feedback: dict[str, Any]) -> None:
        """Collect and log feedback."""
        feedback_obj = Feedback.model_validate(feedback)
        self.logger.log_struct(feedback_obj.model_dump(), severity="INFO")

    def query(self, question: str, context: Optional[dict] = None) -> str:
        """Non-streaming query delegation to HostAgent."""
        import asyncio
        import concurrent.futures
        from app.agent import host_agent_app
        
        async def run_query():
            return await host_agent_app.query(question, context)
            
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(run_query())).result()
        else:
            return asyncio.run(run_query())

    def stream_query(self, *, message, user_id: str, session_id=None, run_config=None, context: Optional[dict] = None, **kwargs):
        """Streaming query delegation to HostAgent (synchronous generator)."""
        import asyncio
        import queue
        
        global MAIN_LOOP
        loop = MAIN_LOOP
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                    
        if not loop:
            loop = asyncio.new_event_loop()
            
        q = queue.Queue()
        DONE = object()
        
        async def run_and_enqueue():
            try:
                async for chunk in self.async_stream_query(
                    message=message,
                    user_id=user_id,
                    session_id=session_id,
                    run_config=run_config,
                    context=context,
                    **kwargs
                ):
                    q.put(chunk)
            except Exception as e:
                q.put(e)
            finally:
                q.put(DONE)
                
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(run_and_enqueue(), loop)
        else:
            # If the loop is not running on this thread, we can run it
            loop.run_until_complete(run_and_enqueue())
            
        while True:
            item = q.get()
            if item is DONE:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    async def async_stream_query(self, *, message, user_id: str, session_id=None, session_events=None, run_config=None, context: Optional[dict] = None, **kwargs):
        """Override to initialize RemoteContext, load trajectory, and inject dynamic system instructions for streaming."""
        # --- FAST-PATH ACTION INTERCEPTOR ---
        if message and message.startswith("/action switchHub"):
            parts = message.split(" ", 2)
            if len(parts) >= 2:
                action_payload = {}
                if len(parts) == 3:
                    try:
                        import json
                        action_payload = json.loads(parts[2])
                    except Exception:
                        pass
                target_hub = action_payload.get("hubId")
                if target_hub:
                    yield {
                        "content": {
                            "parts": [{"text": f"Switching context to hub: {target_hub}"}]
                        },
                        "actions": [{
                            "type": "SWITCH_HUB",
                            "payload": {
                                "hubId": target_hub
                            }
                        }]
                    }
                    return
        
        import uuid
        from app.core import hubscape_adk
        from app.agent import root_agent
        from google.genai import types
        from google.adk.agents.run_config import RunConfig, StreamingMode
        from vertexai.agent_engines import _utils
        
        user_id_resolved = (context or {}).get("userId") or (context or {}).get("user_id") or user_id or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        from app.app_utils.env_resolver import get_project_id
        project_id = get_project_id()
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id_resolved,
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=context
        )
        
        if not self.agent_executor:
            self.set_up()
        base_runner = await self.agent_executor._resolve_runner()
        cloned_agent = base_runner.agent.clone()

        system_instruction = (context or {}).get("system_instruction")
        if system_instruction:
            base_skill_instruction = cloned_agent.instruction or ""
            if base_skill_instruction and base_skill_instruction not in system_instruction:
                cloned_agent.instruction = f"[IDENTITY & PERSONA]\n{system_instruction}\n\n[CORE ORCHESTRATION & MEMORY DIRECTIVES]\n{base_skill_instruction}"
            else:
                cloned_agent.instruction = system_instruction

        from google.adk.runners import Runner
        runner = Runner(
            agent=cloned_agent,
            app_name=base_runner.app_name,
            session_service=base_runner.session_service,
            artifact_service=base_runner.artifact_service,
            memory_service=base_runner.memory_service,
            credential_service=base_runner.credential_service,
            auto_create_session=base_runner.auto_create_session
        )
        token = request_runner_ctx.set(runner)

        session_id_resolved = session_id or (context or {}).get("sessionId") or f"session_{user_id_resolved}_{hub_id}"

        # --- TELEMETRY CONTEXT VARIABLES SETTING ---
        telemetry_org_id.set(org_id or "unknown")
        telemetry_hub_id.set(hub_id or "unknown")
        telemetry_user_id.set(user_id_resolved or "unknown")
        telemetry_conversation_id.set(session_id_resolved)

        # --- OPENTELEMETRY CONTEXT ENRICHMENT ---
        try:
            from opentelemetry import trace
            current_span = trace.get_current_span()
            if current_span:
                current_span.set_attribute("org_id", org_id or "unknown")
                current_span.set_attribute("hub_id", hub_id or "unknown")
                current_span.set_attribute("user_id", user_id_resolved or "unknown")
        except Exception as otel_err:
            print(f"⚠️ Failed to set OpenTelemetry span attributes in stream query: {otel_err}")

        try:
            with hubscape_adk.context_session(remote_ctx):
                # Try to restore session trajectory from Firestore using ADK serialization
                try:
                    session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id_resolved)
                    if session_doc and "adk_session" in session_doc:
                        adk_session_json = session_doc["adk_session"]
                        from google.adk.sessions import Session
                        session_obj = Session.model_validate_json(adk_session_json)
                        
                        # Inject loaded session into session service cache
                        session_service = runner.session_service
                        app_name = adk_app.name
                        uid = session_obj.user_id
                        sid = session_obj.id
                        
                        if app_name not in session_service.sessions:
                            session_service.sessions[app_name] = {}
                        if uid not in session_service.sessions[app_name]:
                            session_service.sessions[app_name][uid] = {}
                        session_service.sessions[app_name][uid][sid] = session_obj
                        print(f"🔄 Resumed ADK GEAP Session in async_stream_query: {session_id_resolved}")
                    else:
                        print(f"🌱 Starting New ADK GEAP Session in async_stream_query: {session_id_resolved}")
                except Exception as restore_err:
                    print(f"⚠️ Non-critical: Failed to restore session trajectory: {restore_err}")

                # Pre-turn Memory Bank Search
                memory_service = runner.memory_service
                if memory_service and user_id_resolved and user_id_resolved != "anonymous_user":
                    try:
                        memories = await memory_service.search_memory(
                            app_name='host-agent',
                            user_id=user_id_resolved,
                            query=message
                        )
                        if memories:
                            memory_text = "\n".join([f"- {m.content}" for m in memories if getattr(m, 'content', None)])
                            if memory_text.strip():
                                cloned_agent.instruction += f"\n\n[USER LONG-TERM MEMORIES & PREFERENCES]\n{memory_text}\n"
                                print(f"🧠 Injected {len(memories)} retrieved user memories into stream turn context")
                    except Exception as mem_search_err:
                        print(f"⚠️ Memory search non-critical: {mem_search_err}")

                new_message = types.Content(
                    parts=[types.Part.from_text(text=message)]
                )

                run_cfg = None
                if run_config:
                    if isinstance(run_config, dict):
                        run_cfg = RunConfig.model_validate(run_config)
                    else:
                        run_cfg = run_config
                if not run_cfg:
                    run_cfg = RunConfig(streaming_mode=StreamingMode.SSE)

                async for event in runner.run_async(
                    new_message=new_message,
                    user_id=user_id_resolved,
                    session_id=session_id_resolved,
                    run_config=run_cfg,
                    **kwargs
                ):
                    yield _utils.dump_event_for_json(event)

                # Yield custom actions if any were collected
                actions = getattr(remote_ctx, "actions", [])
                if actions:
                    yield {"actions": actions}

                # Retrieve updated session state, ingest to Memory Bank, and persist back to Firestore
                try:
                    session_service = runner.session_service
                    updated_session = await session_service.get_session(
                        app_name=adk_app.name,
                        user_id=user_id_resolved,
                        session_id=session_id_resolved
                    )
                    if updated_session:
                        serialized_json = updated_session.model_dump_json()
                        remote_ctx.save(
                            scope="user",
                            collection_name="sessions",
                            doc_id=session_id_resolved,
                            data={
                                "adk_session": serialized_json
                            }
                        )
                        print(f"💾 Persisted ADK GEAP Session trajectory for {session_id_resolved}")

                        # Post-turn Memory Bank Ingestion
                        if memory_service:
                            try:
                                await memory_service.add_session_to_memory(updated_session)
                                print(f"🧠 Ingested session turn to VertexAiMemoryBankService for user {user_id_resolved}")
                            except Exception as mem_ingest_err:
                                print(f"⚠️ Memory ingestion non-critical: {mem_ingest_err}")
                except Exception as save_err:
                    print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")
        finally:
            try:
                request_runner_ctx.reset(token)
            except Exception:
                pass

    def get_agent_card(self) -> dict:
        """
        [NEW] Returns the metadata card of the agent and all its tools.
        Used by the platform Host core during GitOps deploys or sync sweeps.
        """
        from app.agent import app as adk_app
        root_agent = getattr(adk_app, "root_agent", None)
        
        extensions = [
            {
                "uri": "https://google.github.io/adk-docs/a2a/a2a-extension/",
                "description": "Ability to use the new agent executor implementation"
            }
        ]
        privileges_data = _load_privileges_without_tools()
        if privileges_data:
            extensions.append({
                "uri": "https://hubscape.io/extensions/privileges",
                "description": "Workspace role-based privileges matrix",
                "params": privileges_data
            })
            
        card_dict = {
            "name": getattr(root_agent, "name", "host-agent"),
            "description": getattr(root_agent, "description", "Host orchestrator agent."),
            "version": "0.1.0",
            "capabilities": {
                "streaming": True,
                "extensions": extensions
            },
            "tools": []
        }
        tools_list = root_agent.tools if root_agent and hasattr(root_agent, "tools") else []
        for tool_obj in tools_list:
            tool_name = getattr(tool_obj, "__name__", str(tool_obj))
            card_dict["tools"].append({
                "name": tool_name,
                "description": tool_obj.__doc__ or ""
            })
        return card_dict

    def register_operations(self) -> dict[str, list[str]]:
        """Registers the operations of the Agent."""
        operations = super().register_operations()
        operations[""] = [
            *operations.get("", []), 
            "register_feedback", 
            "get_agent_card",
            "query",
        ]
        operations["stream"] = ["stream_query"]
        operations["async_stream"] = ["async_stream_query"]
        return operations


    def clone(self) -> "AgentEngineApp":
        """Returns a clone of the Agent Runtime application."""
        return self


gemini_location = os.environ.get("GOOGLE_CLOUD_LOCATION")
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
agent_runtime = AgentEngineApp.create(
    app=adk_app,
    artifact_service=(
        GcsArtifactService(bucket_name=logs_bucket_name)
        if logs_bucket_name
        else InMemoryArtifactService()
    ),
    session_service=InMemorySessionService(),
)
