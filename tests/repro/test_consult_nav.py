import asyncio
import os
import sys

host_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, host_agent_dir)

from app.core import hubscape_adk

ctx = hubscape_adk.RemoteContext(
    user_id="test_user",
    agent_id="host_agent",
    org_id="test_org",
    hub_id="test_hub",
    raw_context={
        "userId": "test_user",
        "sessionId": "test_session_123",
        "accessible_agents": [
            {
                "id": "2eadb391-a7b5-5432-94f8-33f1841213ef",
                "name": "navigation-agent",
                "geap_resource_name": "projects/1097730318341/locations/us-central1/reasoningEngines/3445053054066360320",
                "type": "A2A"
            }
        ]
    }
)

async def main():
    with hubscape_adk.context_session(ctx):
        from app.scripts.consult_agent import consultAgent
        print("Executing consultAgent('navigation-agent', 'How far is TD Garden from Boston Logan Airport by car?')...")
        res = await consultAgent("navigation-agent", "How far is TD Garden from Boston Logan Airport by car?")
        print("ConsultAgent Result:\n", res)

if __name__ == "__main__":
    asyncio.run(main())
