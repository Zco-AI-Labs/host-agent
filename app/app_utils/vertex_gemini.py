import os
import asyncio
import google.auth
from google.adk.models.google_llm import Gemini
from google.genai import Client
from google.genai import types

class VertexGemini(Gemini):
    @property
    def api_client(self) -> Client:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
            
        if not hasattr(self, "_clients_by_loop"):
            self._clients_by_loop = {}
            
        # Clean up closed loops to avoid memory leak
        for lp in list(self._clients_by_loop.keys()):
            if lp is not None and lp.is_closed():
                try:
                    self._clients_by_loop[lp].close()
                except Exception:
                    pass
                del self._clients_by_loop[lp]
                
        if loop not in self._clients_by_loop:
            project = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
            location = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
            try:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            except Exception:
                credentials = None
            self._clients_by_loop[loop] = Client(
                vertexai=True,
                project=project,
                location=location,
                credentials=credentials
            )
            
        return self._clients_by_loop[loop]

    @property
    def _live_api_client(self) -> Client:
        """Avoid closed event loop for live/voice connections by resolving dynamically."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
            
        if not hasattr(self, "_live_clients_by_loop"):
            self._live_clients_by_loop = {}
            
        # Clean up closed loops
        for lp in list(self._live_clients_by_loop.keys()):
            if lp is not None and lp.is_closed():
                try:
                    self._live_clients_by_loop[lp].close()
                except Exception:
                    pass
                del self._live_clients_by_loop[lp]
                
        if loop not in self._live_clients_by_loop:
            project = os.getenv("GOOGLE_CLOUD_PROJECT") or "hubscape-geap"
            location = os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1"
            try:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
            except Exception:
                credentials = None
                
            base_url, _ = self._base_url_and_api_version
            
            self._live_clients_by_loop[loop] = Client(
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
            
        return self._live_clients_by_loop[loop]

def get_model(model_name: str = "gemini-2.5-flash") -> VertexGemini:
    return VertexGemini(model=model_name)


