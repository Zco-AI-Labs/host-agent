from functools import cached_property
import os
import google.auth
from google.adk.models.google_llm import Gemini
from google.genai import Client

class VertexGemini(Gemini):
    @cached_property
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

def get_model(model_name: str = "gemini-2.5-flash") -> VertexGemini:
    return VertexGemini(model=model_name)
