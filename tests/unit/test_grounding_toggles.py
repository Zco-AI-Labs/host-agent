import pytest
import os
import json
import sys

def test_host_agent_grounding_toggles():
    """Test that host-agent loads google_maps tool when config.json has allow_google_maps: true."""
    if "app.agent" in sys.modules:
        del sys.modules["app.agent"]
        
    from app.agent import allow_web_search, allow_google_maps
    assert allow_web_search is True
    assert allow_google_maps is True
