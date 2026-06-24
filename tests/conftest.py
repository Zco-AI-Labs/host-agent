import pytest
from unittest.mock import patch
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.genai import types

async def mock_generate_content_async(self, llm_request, stream=False):
    # Determine mock output text
    text_output = "Hello! I am a mocked Gemini agent response."
    yield LlmResponse(
        content=types.Content(parts=[types.Part.from_text(text=text_output)]),
        turn_complete=True
    )

@pytest.fixture(autouse=True)
def mock_gemini_api():
    """Autouse fixture to mock Gemini API calls across all tests."""
    with patch.object(Gemini, "generate_content_async", new=mock_generate_content_async):
        yield
