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
from typing import Any

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
        vertexai.init()
        setup_telemetry()
        super().set_up()
        logging.basicConfig(level=logging.INFO)
        logging_client = google_cloud_logging.Client()
        self.logger = logging_client.logger(__name__)
        if gemini_location:
            os.environ["GOOGLE_CLOUD_LOCATION"] = gemini_location

    def inspect_env(self) -> str:
        """Inspects environment and credentials."""
        try:
            import google.auth
            credentials, project = google.auth.default(
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            from google.auth.transport.requests import Request
            credentials.refresh(Request())
            token_info = f"Token present: {bool(credentials.token)}"
            if credentials.token:
                token_info += f" (length: {len(credentials.token)}, starts with: {credentials.token[:10]})"
        except Exception as e:
            token_info = f"Failed to load credentials: {e}"

        env_vars = {k: v for k, v in os.environ.items() if not k.endswith("KEY") and "PASSWORD" not in k and "SECRET" not in k}
        
        import sys
        res = f"Python Executable: {sys.executable}\n"
        res += f"Python Path: {sys.path}\n"
        try:
            dir_path = os.path.dirname(os.path.abspath(__file__))
            agent_path = os.path.join(dir_path, "agent.py")
            with open(agent_path, "r") as f:
                agent_lines = [f.readline() for _ in range(30)]
            res += "--- agent.py Contents ---\n" + "".join(agent_lines) + "-------------------------\n"
        except Exception as read_err:
            res += f"Failed to read agent.py: {read_err}\n"
        res += f"Project: {project}\n"
        res += f"Token Info: {token_info}\n"
        res += f"Environment Variables:\n"
        for k, v in env_vars.items():
            res += f"  {k}: {v}\n"
        return res

    def register_feedback(self, feedback: dict[str, Any]) -> None:
        """Collect and log feedback."""
        feedback_obj = Feedback.model_validate(feedback)
        self.logger.log_struct(feedback_obj.model_dump(), severity="INFO")

    def register_operations(self) -> dict[str, list[str]]:
        """Registers the operations of the Agent."""
        operations = super().register_operations()
        operations[""] = [*operations.get("", []), "register_feedback", "inspect_env"]
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
