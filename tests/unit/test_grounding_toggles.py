import pytest
import os
import json
import sys

def test_host_agent_grounding_toggles():
    """Test that host-agent has grounding toggles set to false for pure orchestrator role."""
    if "app.agent" in sys.modules:
        del sys.modules["app.agent"]
        
    from app.agent import allow_web_search, allow_google_maps
    assert allow_web_search is False
    assert allow_google_maps is False
