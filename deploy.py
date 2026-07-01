import os
import sys
import subprocess
import shutil
import json

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

# Fetch project number dynamically using gcloud or metadata server
project_number = None
try:
    project_info = subprocess.run(
        ["gcloud", "projects", "describe", PROJECT_ID, "--format=json"],
        capture_output=True, text=True, check=True
    )
    project_number = json.loads(project_info.stdout).get("projectNumber")
except Exception as e:
    print(f"Warning: Failed to fetch project number via gcloud: {e}")

if not project_number:
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/project/numeric-project-id",
            headers={"Metadata-Flavor": "Google"}
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            project_number = response.read().decode().strip()
    except Exception:
        pass

if not project_number and PROJECT_ID == "hubscape-geap":
    project_number = "1097730318341"

if project_number:
    service_account = f"{project_number}-compute@developer.gserviceaccount.com"
    print(f"Resolved Service Account: {service_account}")
else:
    raise RuntimeError("Failed to resolve project number dynamically for service account.")

cmd = [
    agents_cli_path, "deploy",
    "--project", PROJECT_ID,
    "--region", LOCATION,
    "--service-name", display_name,
    "--service-account", service_account,
    "--no-confirm-project"
]

env = os.environ.copy()
venv_bin = os.path.dirname(sys.executable)
env["PATH"] = f"{venv_bin}{os.path.pathsep}{env.get('PATH', '')}"

print(f"Executing: {' '.join(cmd)}")
subprocess.run(cmd, env=env, check=True)
print("🎉 Deployment completed successfully!")
