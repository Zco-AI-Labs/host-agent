import os
import google.auth
import logging

logger = logging.getLogger(__name__)

def get_project_id() -> str:
    # 1. Try environment variables
    project = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if project:
        return project
    
    # 2. Try google.auth.default()
    try:
        _, project = google.auth.default()
        if project and not project.startswith("agents-global") and "system.id.goog" not in project:
            return project
    except Exception:
        pass
        
    # Default fallback to staging project ID
    return "hubscape-geap"

def get_region() -> str:
    # 1. Try environment variables
    loc = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GCP_LOCATION") or os.getenv("LOCATION")
    if loc:
        return loc
        
    # 2. Try fetching from Metadata Server
    try:
        import httpx as httpx_sync
        resp = httpx_sync.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
            headers={"Metadata-Flavor": "Google"},
            timeout=1.0
        )
        if resp.status_code == 200:
            # Format: projects/12345/zones/us-central1-a
            zone_path = resp.text.strip()
            if "/" in zone_path:
                zone = zone_path.split("/")[-1]
                # Extract region (e.g., us-central1-a -> us-central1)
                region = "-".join(zone.split("-")[:-1])
                if region:
                    return region
    except Exception:
        pass
        
    # Standard default fallback if all lookups fail
    return "us-central1"
