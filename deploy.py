import os
import sys
import subprocess
import shutil

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "hubscape-geap")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
display_name = "host-agent"

print(f"Deploying {display_name} via native agents-cli...")

agents_cli_path = shutil.which("agents-cli")
if not agents_cli_path:
    venv_bin = os.path.dirname(sys.executable)
    fallback_path = os.path.join(venv_bin, "agents-cli")
    if os.path.exists(fallback_path):
        agents_cli_path = fallback_path
if not agents_cli_path:
    agents_cli_path = "agents-cli"

iam_profile = None
try:
    try:
        from google.cloud import firestore
    except ImportError:
        print("ℹ️ google-cloud-firestore not found. Installing dynamically...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "google-cloud-firestore"], check=True)
        from google.cloud import firestore

    db = firestore.Client(project=PROJECT_ID)
    docs = db.collection("agents").where("name", "==", display_name).limit(1).stream()
    doc = next(docs, None)
    if doc:
        iam_profile = doc.to_dict().get("iam_profile")
        if iam_profile:
            print(f"ℹ️ Found agent configuration in Firestore. Binding profile: {iam_profile}")
        else:
            print("ℹ️ Found agent in Firestore, but iam_profile is not set. Defaulting to AGENT_IDENTITY")
    else:
        print("ℹ️ Agent not found in Firestore. Defaulting to AGENT_IDENTITY")
except Exception as e:
    print(f"⚠️ Could not fetch agent profile from Firestore ({e}). Defaulting to AGENT_IDENTITY")

cmd = [
    agents_cli_path, "deploy",
    "--project", PROJECT_ID,
    "--region", LOCATION,
    "--service-name", display_name,
    "--no-confirm-project"
]

if iam_profile:
    cmd.extend(["--service-account", f"{iam_profile}@{PROJECT_ID}.iam.gserviceaccount.com"])
else:
    cmd.append("--agent-identity")

env = os.environ.copy()
venv_bin = os.path.dirname(sys.executable)
env["PATH"] = f"{venv_bin}{os.path.pathsep}{env.get('PATH', '')}"

print(f"Executing: {' '.join(cmd)}")
subprocess.run(cmd, env=env, check=True)
print("🎉 Deployment completed successfully!")

