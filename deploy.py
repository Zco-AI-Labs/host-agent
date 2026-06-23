import os
import uuid
import subprocess
import vertexai
from vertexai.preview import reasoning_engines

# Resolve agent application dynamically from agent.py
agent_app = None
display_name = "custom-agent"
description_prefix = "Managed GEAP agent."

try:
    import agent
    for attr in ["host_agent_app", "todo_agent_app", "simple_form_agent_app"]:
        if hasattr(agent, attr):
            agent_app = getattr(agent, attr)
            display_name = attr.replace("_app", "").replace("_", "-")
            if "host" in attr:
                description_prefix = "Managed GEAP Host Orchestrator."
            elif "todo" in attr:
                description_prefix = "Managed GEAP agent for user personal to-do lists."
            else:
                description_prefix = "Managed GEAP agent to display and capture contact support forms."
            break
    if not agent_app:
        for attr in dir(agent):
            if attr.endswith("_app"):
                agent_app = getattr(agent, attr)
                display_name = attr.replace("_app", "").replace("_", "-")
                description_prefix = f"Managed GEAP custom agent ({display_name})."
                break
except Exception as e:
    raise ImportError(f"Failed to import agent app from agent.py: {e}")

if not agent_app:
    raise ImportError("Could not find any agent app instance (ending with '_app') in agent.py")

# Resolve environment configuration
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "hubscape-geap")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")

def get_repo_url():
    # Check environment variable first (CI/CD)
    github_repo = os.getenv("GITHUB_REPOSITORY")
    if github_repo:
        return f"https://github.com/{github_repo}"
    
    # Fallback to local git config
    try:
        url = subprocess.check_output(["git", "config", "--get", "remote.origin.url"], text=True).strip()
        if url.endswith(".git"):
            url = url[:-4]
        if url.startswith("git@github.com:"):
            url = "https://github.com/" + url[len("git@github.com:"):]
        return url
    except Exception:
        # Default fallback
        return f"https://github.com/Zco-AI-Labs/{display_name}"

# Calculate deterministic agent UUID from the repository URL
repo_url = get_repo_url()
agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, repo_url))
uuid_token = f"[agent_uuid: {agent_uuid}]"
description = f"{description_prefix} {uuid_token}"

print(f"Initializing Vertex AI (Project: {PROJECT_ID}, Location: {LOCATION})...")
vertexai.init(project=PROJECT_ID, location=LOCATION)

# Search for an existing reasoning engine deployment matching the UUID
existing_engine_id = None
try:
    print(f"Checking for existing deployment matching UUID {agent_uuid}...")
    all_engines = reasoning_engines.ReasoningEngine.list()
    for engine in all_engines:
        gca_res = getattr(engine, "gca_resource", None)
        engine_desc = getattr(gca_res, "description", "") or ""
        if uuid_token in engine_desc:
            existing_engine_id = engine.resource_name.split('/')[-1]
            print(f"Found existing engine instance: {engine.resource_name} (ID: {existing_engine_id})")
            break
except Exception as list_err:
    print(f"⚠️ Non-critical: Failed to check for existing engines: {list_err}")

# Build and execute the adk deploy agent_engine command
print(f"Deploying A2A-compliant {display_name} container to Agent Engine...")

cmd = [
    "python", "-m", "google.adk.cli", "deploy", "agent_engine",
    "--project", PROJECT_ID,
    "--region", LOCATION,
    "--display_name", display_name,
    "--description", description,
]

if existing_engine_id:
    cmd.extend(["--agent_engine_id", existing_engine_id])

# Run from the current repository root directory
cmd.append(".")

print(f"Running command: {' '.join(cmd)}")
subprocess.run(cmd, check=True)

print("\n🎉 Deployment Successful!")

# Post-Deployment Cleanup: Delete other deployments of the same agent (matching UUID in description)
try:
    print(f"\n🧹 Checking for any stale {display_name} deployments on GCP matching UUID {agent_uuid}...")
    all_engines = reasoning_engines.ReasoningEngine.list()
    matching_engines = []
    for engine in all_engines:
        gca_res = getattr(engine, "gca_resource", None)
        engine_desc = getattr(gca_res, "description", "") or ""
        if uuid_token in engine_desc:
            matching_engines.append(engine)
    
    if len(matching_engines) > 1:
        # Sort by update_time descending (newest first)
        matching_engines.sort(key=lambda x: x.gca_resource.update_time, reverse=True)
        active_engine = matching_engines[0]
        print(f"Active engine instance: {active_engine.resource_name}")
        for engine in matching_engines[1:]:
            print(f"Deleting stale engine instance: {engine.resource_name}...")
            engine.delete()
            print(f"Successfully deleted {engine.resource_name}.")
    print("✨ Cleanup complete!")
except Exception as cleanup_err:
    print(f"⚠️ Non-critical error during old deployment cleanup: {cleanup_err}")
