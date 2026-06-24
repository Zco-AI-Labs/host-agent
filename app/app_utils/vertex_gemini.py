from functools import cached_property
import os
from google.adk.models.google_llm import Gemini
from google.genai import Client

class VertexGemini(Gemini):
    @cached_property
    def api_client(self) -> Client:
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
        location = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
        return Client(vertexai=True, project=project, location=location)

def get_model(model_name: str = "gemini-2.5-flash") -> VertexGemini:
    return VertexGemini(model=model_name)
