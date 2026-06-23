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
            display_name = attr.replace("_app", "")
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
                display_name = attr.replace("_app", "")
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
            # Check if this engine was deployed with package_spec (legacy pickled)
            spec = getattr(gca_res, "spec", None)
            is_legacy = False
            if spec:
                pb_msg = getattr(spec, "_pb", None)
                if pb_msg and hasattr(pb_msg, "ListFields"):
                    is_legacy = any(f.name == "package_spec" for f, _ in pb_msg.ListFields())
                elif hasattr(spec, "package_spec") and spec.package_spec:
                    if getattr(spec.package_spec, "pickle_object_gcs_uri", None):
                        is_legacy = True
            
            if is_legacy:
                print(f"Found existing engine instance {engine.resource_name} but it is a legacy pickled deployment.")
                print("We cannot update legacy pickled deployments in-place. We will deploy a new instance and clean up the legacy one.")
            else:
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

# Post-Deployment A2A Registry Update
try:
    print("\n🌐 Synchronizing A2A Service in Google Cloud Agent Registry...")
    import requests
    import google.auth
    import google.auth.transport.requests
    from starlette.testclient import TestClient
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    # 1. Resolve credentials
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)
    token = credentials.token

    # 2. Get active engine ID (query Vertex AI to find the latest matching deployment)
    print("Resolving latest deployed Reasoning Engine ID...")
    active_engine_id = None
    all_engines = reasoning_engines.ReasoningEngine.list()
    for engine in all_engines:
        gca_res = getattr(engine, "gca_resource", None)
        engine_desc = getattr(gca_res, "description", "") or ""
        if uuid_token in engine_desc:
            active_engine_id = engine.resource_name.split('/')[-1]
            break

    if not active_engine_id:
        raise ValueError(f"Could not find any deployed engine matching UUID {agent_uuid}")

    # 3. Generate A2A Agent Card using the ADK starlette utility
    print("Generating A2A Agent Card...")
    # Import agent app root_agent safely
    adk_agent = getattr(agent, "root_agent", None)
    if not adk_agent:
        raise ValueError("Could not find root_agent in agent.py")

    app = to_a2a(adk_agent)
    with TestClient(app) as client:
        card = client.get('/.well-known/agent-card.json').json()

    engine_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{active_engine_id}"
    card['url'] = engine_url

    # 4. Create or update the Service resource in the Agent Registry
    service_id = f"{display_name.replace('_', '-')}-a2a"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Check if the service already exists
    get_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/services/{service_id}"
    res = requests.get(get_url, headers=headers)
    exists = (res.status_code == 200)

    payload = {
        "displayName": display_name,
        "description": description_prefix,
        "agentSpec": {
            "type": "A2A_AGENT_CARD",
            "content": card
        }
    }

    if exists:
        print(f"Service {service_id} exists. Updating Spec...")
        patch_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/services/{service_id}?updateMask=displayName,description,agentSpec"
        res = requests.patch(patch_url, json=payload, headers=headers)
    else:
        print(f"Service {service_id} does not exist. Creating...")
        post_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{PROJECT_ID}/locations/{LOCATION}/services?serviceId={service_id}"
        res = requests.post(post_url, json=payload, headers=headers)

    if res.status_code in (200, 201):
        print(f"✅ Successfully registered {service_id} as A2A service in GCP Agent Registry!")
    else:
        print(f"⚠️ Agent Registry Sync returned non-success status {res.status_code}: {res.text}")

except Exception as registry_err:
    print(f"⚠️ Non-critical: Failed to sync with GCP Agent Registry: {registry_err}")

