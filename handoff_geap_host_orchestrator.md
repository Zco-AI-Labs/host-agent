# Handoff: Deploying the Central GEAP Host Orchestrator

## 🎯 Objective
Establish, package, and deploy the central **Host Agent (Orchestrator)** as a remote Vertex AI Reasoning Engine (GEAP) in the Google Cloud project (`hubscape-geap`). This agent will serve as the entry gateway for the Hubscape platform, routing user requests to specialized sub-agents (like `todo-agent`) using dynamic metadata whitelists.

---

## 📡 Current Backend Architecture (The Proxy Relay)
The FastAPI backend (`backend_python`) has been completely decoupled from LLM execution:
* **No Local LLM Calls:** All direct Gemini SDK (`google-genai`) references and the legacy local ReAct runner (`runner.py`) have been decommissioned.
* **Pure Proxy Gateway:** The backend receives chat requests at `/api/host/chat` and forwards them directly to the remote GEAP Host Agent.
* **Network Error Handling:** If the connection to the remote Host Agent fails or is unconfigured, the backend yields a standard link connection error to the client UI.

### Context Injection Payload
For every chat request, the backend scans Firestore and dynamically injects a JSON context payload into the `input.context` field of the Vertex AI REST request:
```json
{
  "mode": "chat_pc",
  "userId": "platform_user_uuid",
  "firebaseUid": "firebase_auth_uid",
  "email": "user@example.com",
  "hubId": "active_hub_uuid",
  "orgId": "active_org_uuid",
  "userRoles": ["member", "hub_admin"],
  "accessible_agents": [
    {
      "id": "todo_agent",
      "name": "Todo Agent",
      "description": "Manages tasks and TODO lists.",
      "type": "GEAP",
      "geap_resource_name": "projects/1097730318341/locations/us-central1/reasoningEngines/3604100709559042048"
    }
  ]
}
```

---

## 🧠 GEAP Host Orchestrator Requirements

To build this Host Agent on the GEAP side, the packaging configuration must define:

### 1. System Prompt / Persona
The Host Agent must act as the primary interface/gatekeeper:
* **Triage & Routing:** Read the `accessible_agents` list in the incoming context. If the user's request maps to a sub-agent's description (e.g. scheduling a meeting, listing tasks), invoke the `consultAgent` tool.
* **Security Compliance:** Under no circumstances should the Host Agent attempt to contact or invoke a sub-agent that is *not* present in the `accessible_agents` list.
* **Direct Interaction:** Handle general greetings, navigation requests, or platform support queries directly.

### 2. Required Tools
* **`consultAgent(agent_id: str, query: str) -> str`**: Invokes the target sub-agent's Reasoning Engine (via its `geap_resource_name` resolved from the whitelist) and returns its response.
* **`searchKnowledge(query: str) -> str`**: Queries the central Vector database/RAG indexes for static platform documentation.
* **`switchHub(hub_id: str) -> str`**: Triggers navigation actions to move the user's workspace context.

---

## 🛠️ Next Steps in the New Chat

1. **Create Host Agent Package:** Define a python class/package representing the Host Orchestrator (e.g. `HostOrchestrator`) that conforms to the Vertex AI Reasoning Engine SDK requirements.
2. **Implement `query` method:**
   * Define the signature to accept the `context` dictionary:
     ```python
     def query(self, question: str, context: Optional[dict] = None) -> str:
     ```
   * Parse the user metadata and whitelisted `accessible_agents` from the context.
3. **Write Tool Handlers:** Implement the local Python functions for the tools (`consultAgent`, `searchKnowledge`, `switchHub`) that will run inside the GEAP container.
4. **Deploy to Vertex AI:** Use the Vertex AI SDK to register and deploy the Host Orchestrator to the `hubscape-geap` GCP project:
   ```python
   from google.cloud import aiplatform
   aiplatform.init(project="hubscape-geap", location="us-central1")
   
   # Deploy reasoning engine
   engine = aiplatform.ReasoningEngine.create(
       HostOrchestrator(),
       requirements=["google-cloud-aiplatform", "httpx"],
       display_name="host-orchestrator"
   )
   ```
5. **Configure local `.env`:** Copy the generated resource name (e.g., `projects/1097730318341/locations/us-central1/reasoningEngines/XYZ`) and set it as `GEAP_HOST_RESOURCE` in the FastAPI backend's `.env` file.
