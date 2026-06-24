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
import logging
import os
from typing import Any, Optional, Dict, List, Union

import vertexai
from dotenv import load_dotenv
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.cloud import logging as google_cloud_logging
from vertexai.agent_engines.templates.adk import AdkApp

from app.agent import app as adk_app
from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

# Load environment variables from .env file at runtime
load_dotenv()


class AgentEngineApp(AdkApp):
    def set_up(self) -> None:
        """Initialize the agent engine app with logging and telemetry."""
        # Explicitly pop GOOGLE_GENAI_USE_ENTERPRISE and set GOOGLE_GENAI_USE_VERTEXAI to force regional Vertex AI routing
        os.environ.pop("GOOGLE_GENAI_USE_ENTERPRISE", None)
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
        if gemini_location:
            os.environ["GOOGLE_CLOUD_LOCATION"] = gemini_location
        vertexai.init()
        setup_telemetry()
        super().set_up()
        if "runner" in self._tmpl_attrs:
            self._tmpl_attrs["runner"].auto_create_session = True
        if "in_memory_runner" in self._tmpl_attrs:
            self._tmpl_attrs["in_memory_runner"].auto_create_session = True
        logging.basicConfig(level=logging.INFO)
        logging_client = google_cloud_logging.Client()
        self.logger = logging_client.logger(__name__)

    def inspect_env(self) -> str:
        """Inspects environment, credentials, and attempts a direct Gemini call."""
        import traceback
        import sys
        import os
        
        token_info = ""
        try:
            import google.auth
            credentials, project = google.auth.default(
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            token_info = f"Token present: {bool(credentials.token)} (Class: {credentials.__class__.__name__})"
        except Exception as e:
            token_info = f"Failed to load credentials: {e}"

        direct_call_status = ""
        try:
            from google.genai import Client
            proj_id = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
            loc = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
            client = Client(vertexai=True, project=proj_id, location=loc)
            resp = client.models.generate_content(model="gemini-2.5-flash", contents="Hi")
            direct_call_status = f"SUCCESS: {resp.text[:30]}..."
        except Exception as e:
            direct_call_status = f"FAILED: {e.__class__.__name__}: {e}\n{traceback.format_exc()}"

        env_vars = {k: v for k, v in os.environ.items() if not k.endswith("KEY") and "PASSWORD" not in k and "SECRET" not in k}
        
        res = f"Python Executable: {sys.executable}\n"
        res += f"Token Info: {token_info}\n"
        res += f"Direct Call Status: {direct_call_status}\n"
        res += f"Environment Variables:\n"
        for k, v in env_vars.items():
            res += f"  {k}: {v}\n"
        return res

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

    def stream_query(self, *, message, user_id: str, session_id=None, run_config=None, **kwargs):
        """Override to initialize RemoteContext, load trajectory, and inject dynamic system instructions."""
        context = kwargs.pop("context", None)
        
        import uuid
        import asyncio
        import concurrent.futures
        from app import hubscape_adk
        from app.agent import root_agent
        
        user_id_resolved = (context or {}).get("userId") or (context or {}).get("user_id") or user_id or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        project_id = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or "hubscape-geap"
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id_resolved,
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=context
        )
        
        system_instruction = (context or {}).get("system_instruction")
        if system_instruction:
            root_agent.instruction = system_instruction

        session_id_resolved = session_id or (context or {}).get("sessionId") or f"session_{user_id_resolved}_{hub_id}"

        with hubscape_adk.context_session(remote_ctx):
            # Try to restore session trajectory from Firestore using ADK serialization
            try:
                session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id_resolved)
                if session_doc and "adk_session" in session_doc:
                    adk_session_json = session_doc["adk_session"]
                    from google.adk.sessions import Session
                    session_obj = Session.model_validate_json(adk_session_json)
                    
                    # Inject loaded session into session service cache
                    session_service = self._tmpl_attrs.get("session_service")
                    app_name = self.app.name
                    uid = session_obj.user_id
                    sid = session_obj.id
                    
                    if app_name not in session_service.sessions:
                        session_service.sessions[app_name] = {}
                    if uid not in session_service.sessions[app_name]:
                        session_service.sessions[app_name][uid] = {}
                    session_service.sessions[app_name][uid][sid] = session_obj
                    print(f"🔄 Resumed ADK GEAP Session in stream_query: {session_id_resolved}")
                else:
                    print(f"🌱 Starting New ADK GEAP Session in stream_query: {session_id_resolved}")
            except Exception as restore_err:
                print(f"⚠️ Non-critical: Failed to restore session trajectory: {restore_err}")

            # Execute generator
            yield from super().stream_query(
                message=message,
                user_id=user_id,
                session_id=session_id_resolved,
                run_config=run_config,
                **kwargs,
            )

            # Retrieve updated session state and persist back to Firestore
            try:
                session_service = self._tmpl_attrs.get("session_service")
                async def fetch_session():
                    return await session_service.get_session(
                        app_name=self.app.name,
                        user_id=user_id_resolved,
                        session_id=session_id_resolved
                    )
                
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        updated_session = executor.submit(lambda: asyncio.run(fetch_session())).result()
                else:
                    updated_session = asyncio.run(fetch_session())

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
                print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")

    async def async_stream_query(self, *, message, user_id: str, session_id=None, session_events=None, run_config=None, **kwargs):
        """Override to initialize RemoteContext, load trajectory, and inject dynamic system instructions."""
        context = kwargs.pop("context", None)
        
        import uuid
        from app import hubscape_adk
        from app.agent import root_agent
        
        user_id_resolved = (context or {}).get("userId") or (context or {}).get("user_id") or user_id or "anonymous_user"
        org_id = (context or {}).get("orgId") or (context or {}).get("org_id")
        hub_id = (context or {}).get("hubId") or (context or {}).get("hub_id")
        
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        project_id = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or "hubscape-geap"
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id_resolved,
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=project_id,
            raw_context=context
        )
        
        system_instruction = (context or {}).get("system_instruction")
        if system_instruction:
            root_agent.instruction = system_instruction

        session_id_resolved = session_id or (context or {}).get("sessionId") or f"session_{user_id_resolved}_{hub_id}"

        with hubscape_adk.context_session(remote_ctx):
            # Try to restore session trajectory from Firestore using ADK serialization
            try:
                session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id_resolved)
                if session_doc and "adk_session" in session_doc:
                    adk_session_json = session_doc["adk_session"]
                    from google.adk.sessions import Session
                    session_obj = Session.model_validate_json(adk_session_json)
                    
                    # Inject loaded session into session service cache
                    session_service = self._tmpl_attrs.get("session_service")
                    app_name = self.app.name
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

            async for event in super().async_stream_query(
                message=message,
                user_id=user_id,
                session_id=session_id_resolved,
                session_events=session_events,
                run_config=run_config,
                **kwargs,
            ):
                yield event

            # Retrieve updated session state and persist back to Firestore
            try:
                session_service = self._tmpl_attrs.get("session_service")
                updated_session = await session_service.get_session(
                    app_name=self.app.name,
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
                print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")

    def register_operations(self) -> dict[str, list[str]]:
        """Registers the operations of the Agent."""
        operations = super().register_operations()
        operations[""] = [*operations.get("", []), "register_feedback", "inspect_env", "query"]
        return operations

    def clone(self) -> "AgentEngineApp":
        """Returns a clone of the Agent Runtime application."""
        return self


gemini_location = os.environ.get("GOOGLE_CLOUD_LOCATION")
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
agent_runtime = AgentEngineApp(
    app=adk_app,
    artifact_service_builder=lambda: (
        GcsArtifactService(bucket_name=logs_bucket_name)
        if logs_bucket_name
        else InMemoryArtifactService()
    ),
    session_service_builder=lambda: InMemorySessionService(),
)
