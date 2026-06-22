import os
import uuid
import subprocess
import vertexai
from vertexai.preview import reasoning_engines
from agent import host_agent_app

# Resolve environment configuration
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "hubscape-geap")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
STAGING_BUCKET = os.getenv("GCP_STAGING_BUCKET", "gs://hubscape-geap-reasoning-engines")
if STAGING_BUCKET and not STAGING_BUCKET.startswith("gs://"):
    STAGING_BUCKET = f"gs://{STAGING_BUCKET}"

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
        return "https://github.com/Zco-AI-Labs/host-agent"

# Calculate deterministic agent UUID from the repository URL
repo_url = get_repo_url()
agent_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, repo_url))

print(f"Initializing Vertex AI (Project: {PROJECT_ID}, Location: {LOCATION}, Bucket: {STAGING_BUCKET})...")
vertexai.init(project=PROJECT_ID, location=LOCATION, staging_bucket=STAGING_BUCKET)

print(f"Deploying host-agent (UUID: {agent_uuid}) to GEAP Agent Registry...")
reasoning_engine = reasoning_engines.ReasoningEngine.create(
    host_agent_app,
    requirements=[
        "google-antigravity",
        "google-cloud-aiplatform",
        "google-cloud-firestore",
        "cloudpickle==3.0.0",
        "httpx",
        "pydantic>=2.0"
    ],
    extra_packages=["scripts", "agent.py", "hubscape_adk.py"], # Packages the local script tools, agent class, and adk proxy
    display_name="host-agent",
    description=f"Managed GEAP Host Orchestrator. [agent_uuid: {agent_uuid}]"
)

print("\n🎉 Deployment Successful!")
print(f"GEAP Resource Name: {reasoning_engine.resource_name}")

# Post-Deployment Cleanup: Delete older deployments of the same agent (matching UUID in description)
try:
    uuid_token = f"[agent_uuid: {agent_uuid}]"
    print(f"\n🧹 Cleaning up older host-agent deployments on GCP matching UUID {agent_uuid}...")
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
