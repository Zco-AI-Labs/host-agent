import pytest
import os
import json
import sys

def test_host_agent_grounding_toggles():
    """Test that host-agent loads google_maps tool when config.json has allow_google_maps: true."""
    if "app.agent" in sys.modules:
        del sys.modules["app.agent"]
        
    from app.agent import grounding_agent
    assert grounding_agent is not None
    tool_names = [getattr(t, "name", str(t)) for t in grounding_agent.tools]
    
    assert "google_maps" in tool_names
    assert "google_search" in tool_names
