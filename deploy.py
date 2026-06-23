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
STAGING_BUCKET = os.getenv("GCP_STAGING_BUCKET", "gs://hubscape-geap-reasoning-engines")
if STAGING_BUCKET and not STAGING_BUCKET.startswith("gs://"):
    STAGING_BUCKET = f"gs://{STAGING_BUCKET}"
if STAGING_BUCKET:
    STAGING_BUCKET = f"{STAGING_BUCKET}/{display_name}"

if not STAGING_BUCKET:
    raise ValueError("GCP_STAGING_BUCKET environment variable must be set.")

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

# Dynamically construct extra_packages based on what exists in the repository
extra_packages = []
# 1. Standard Shared Files
for item in ["agent.py", "hubscape_adk.py"]:
    if os.path.exists(item):
        extra_packages.append(item)

# 2. Method A (Flat / Code-Centric) Files
for item in ["prompt.py", "tools.py", "api.py", "api_client.py", "services.py"]:
    if os.path.exists(item):
        extra_packages.append(item)

# 3. Method B (Segregated / Decoupled) Files & Directories
for item in ["SKILL.md", "scripts", "references", "widgets"]:
    if os.path.exists(item):
        extra_packages.append(item)

print(f"Initializing Vertex AI (Project: {PROJECT_ID}, Location: {LOCATION}, Bucket: {STAGING_BUCKET})...")
vertexai.init(project=PROJECT_ID, location=LOCATION, staging_bucket=STAGING_BUCKET)

print(f"Deploying {display_name} (UUID: {agent_uuid}) to GEAP Agent Registry...")
print(f"Packaged files/directories (extra_packages): {extra_packages}")

reasoning_engine = reasoning_engines.ReasoningEngine.create(
    agent_app,
    requirements=[
        "google-adk",
        "google-cloud-aiplatform",
        "google-cloud-firestore",
        "cloudpickle==3.0.0",
        "httpx",
        "pydantic>=2.0"
    ],
    extra_packages=extra_packages,
    display_name=display_name,
    description=f"{description_prefix} [agent_uuid: {agent_uuid}]"
)

print("\n🎉 Deployment Successful!")
print(f"GEAP Resource Name: {reasoning_engine.resource_name}")

# Post-Deployment Cleanup: Delete older deployments of the same agent (matching UUID in description)
try:
    uuid_token = f"[agent_uuid: {agent_uuid}]"
    print(f"\n🧹 Cleaning up older {display_name} deployments on GCP matching UUID {agent_uuid}...")
    all_engines = reasoning_engines.ReasoningEngine.list()
    for engine in all_engines:
        engine_desc = getattr(engine, "description", "") or ""
        if uuid_token in engine_desc and engine.resource_name != reasoning_engine.resource_name:
            print(f"Deleting stale engine instance: {engine.resource_name}...")
            engine.delete()
            print(f"Successfully deleted {engine.resource_name}.")
    print("✨ Cleanup complete!")
except Exception as cleanup_err:
    print(f"⚠️ Non-critical error during old deployment cleanup: {cleanup_err}")
