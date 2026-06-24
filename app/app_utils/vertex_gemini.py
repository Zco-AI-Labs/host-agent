import os
import google.auth
from google.adk.models.google_llm import Gemini
from google.genai import Client
from google.genai import types

class VertexGemini(Gemini):
    @property
    def api_client(self) -> Client:
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
        location = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except Exception:
            credentials = None
        return Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=credentials
        )

    @property
    def _live_api_client(self) -> Client:
        """Avoid closed event loop for live/voice connections by resolving dynamically."""
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
        location = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        except Exception:
            credentials = None
            
        base_url, _ = self._base_url_and_api_version
        
        return Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=credentials,
            http_options=types.HttpOptions(
                headers=self._tracking_headers(),
                api_version=self._live_api_version,
                base_url=base_url,
            )
        )

def get_model(model_name: str = "gemini-2.5-flash") -> VertexGemini:
    return VertexGemini(model=model_name)

