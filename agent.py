import os
import asyncio
import importlib.util
import re
import json
from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
from google.antigravity.types import BuiltinTools, CustomSystemInstructions
from google.antigravity.hooks import policy

def load_local_tools(scripts_dir: str) -> list:
    tools = []
    if not os.path.exists(scripts_dir):
        return tools
    for filename in os.listdir(scripts_dir):
        if filename.endswith(".py") and not filename.startswith("_"):
            module_name = filename[:-3]
            file_path = os.path.join(scripts_dir, filename)
            try:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    func = getattr(module, module_name, None)
                    if not func:
                        # Try camelCase conversion (e.g. consult_agent -> consultAgent)
                        parts = module_name.split("_")
                        camel_name = parts[0] + "".join(p.capitalize() for p in parts[1:])
                        func = getattr(module, camel_name, None)
                    if func and callable(func):
                        tools.append(func)
            except Exception:
                pass
    return tools

class HostAgent:
    def __init__(self):
        # Initialize configuration with empty properties.
        # We will load paths, tools, and system prompt dynamically at runtime.
        self.config = LocalAgentConfig(
            skills_paths=[],
            workspaces=[],
            tools=[],
            capabilities=CapabilitiesConfig(
                disabled_tools=[
                    BuiltinTools.LIST_DIR,
                    BuiltinTools.SEARCH_DIR,
                    BuiltinTools.FIND_FILE,
                    BuiltinTools.VIEW_FILE,
                    BuiltinTools.CREATE_FILE,
                    BuiltinTools.EDIT_FILE,
                    BuiltinTools.RUN_COMMAND,
                    BuiltinTools.GENERATE_IMAGE,
                    BuiltinTools.START_SUBAGENT,
                    BuiltinTools.ASK_QUESTION
                ]
            ),
            policies=[policy.allow_all()],
            vertex=True,
            project=os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or "hubscape-geap",
            location=os.getenv("GCP_LOCATION") or os.getenv("LOCATION") or "us-central1",
            model="gemini-2.5-flash"
        )

    async def query(self, question: str, context: dict = None) -> str:
        """
        Interface method invoked by GEAP / Vertex AI Reasoning Engines.
        """
        runtime_dir = os.path.dirname(os.path.abspath(__file__))
        
        # --- DEBUG HOOK ---
        if question == "debug_env":
            files = []
            for root, dirs, ffiles in os.walk(runtime_dir):
                for f in ffiles:
                    files.append(os.path.relpath(os.path.join(root, f), runtime_dir))
            return f"HostAgent Runtime Dir: {runtime_dir}\nFiles:\n" + "\n".join(files)
        # --- END DEBUG HOOK ---

        scripts_dir = os.path.join(runtime_dir, "scripts")
        
        # Load the custom system instruction passed dynamically from the backend context
        system_instruction = (context or {}).get("system_instruction")
        if system_instruction:
            self.config.system_instructions = CustomSystemInstructions(text=system_instruction)
        else:
            self.config.system_instructions = CustomSystemInstructions(
                text="You are the Hubscape central Host agent."
            )
        
        # Load local python scripts as tools
        self.config.tools = load_local_tools(scripts_dir)
        self.config.skills_paths = [runtime_dir]
        self.config.workspaces = []
        
        import hubscape_adk
        import uuid
        user_id = (context or {}).get("userId") or "anonymous_user"
        org_id = (context or {}).get("orgId")
        hub_id = (context or {}).get("hubId")
        
        # Calculate stable host-agent UUID
        agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Zco-AI-Labs/host-agent"))
        
        remote_ctx = hubscape_adk.RemoteContext(
            user_id=user_id, 
            agent_id=agent_uuid,
            org_id=org_id,
            hub_id=hub_id,
            project_id=self.config.project,
            raw_context=context
        )
        
        # Resolve session ID
        session_id = (context or {}).get("sessionId")
        if not session_id:
            session_id = f"session_{user_id}_{hub_id}"
            
        with hubscape_adk.context_session(remote_ctx):
            # Try to restore session trajectory from Firestore
            conv_id = None
            db_path = None
            try:
                session_doc = remote_ctx.get(scope="user", collection_name="sessions", doc_id=session_id)
                if session_doc and "trajectory" in session_doc and "conversation_id" in session_doc:
                    conv_id = session_doc["conversation_id"]
                    trajectory_bytes = session_doc["trajectory"]
                    
                    # Handle if firestore Blob wrapper is returned
                    if hasattr(trajectory_bytes, "value"):
                        trajectory_bytes = trajectory_bytes.value
                        
                    db_path = f"/tmp/{conv_id}.db"
                    with open(db_path, "wb") as f:
                        f.write(trajectory_bytes)
                        
                    self.config.conversation_id = conv_id
                    self.config.save_dir = "/tmp"
                    print(f"🔄 Resuming GEAP Session: {session_id} (Internal ID: {conv_id})")
                else:
                    self.config.save_dir = "/tmp"
                    self.config.conversation_id = None
                    print(f"🌱 Starting New GEAP Session: {session_id}")
            except Exception as restore_err:
                print(f"⚠️ Non-critical: Failed to restore session trajectory: {restore_err}")
                self.config.save_dir = "/tmp"
                self.config.conversation_id = None
                
            async with Agent(config=self.config) as agent:
                response = await agent.chat(question)
                await response.resolve()
                text_response = await response.text()
                
                # Retrieve active conversation ID and persist updated SQLite DB back to Firestore
                active_conv_id = agent.conversation_id
                if active_conv_id:
                    try:
                        active_db_path = f"/tmp/{active_conv_id}.db"
                        if os.path.exists(active_db_path):
                            with open(active_db_path, "rb") as f:
                                updated_bytes = f.read()
                            remote_ctx.save(
                                scope="user",
                                collection_name="sessions",
                                doc_id=session_id,
                                data={
                                    "trajectory": updated_bytes,
                                    "conversation_id": active_conv_id
                                }
                            )
                            print(f"💾 Persisted GEAP Session trajectory for {session_id} (Internal ID: {active_conv_id})")
                    except Exception as save_err:
                        print(f"⚠️ Non-critical: Failed to save session trajectory: {save_err}")
                
                # Fetch any actions collected during the context session
                actions = getattr(remote_ctx, "actions", [])
                
                # Return the result as a structured JSON string
                return json.dumps({
                    "text": text_response,
                    "actions": actions
                })

# Singleton instance used as the serialization target
host_agent_app = HostAgent()
