import os
import sys

def inspect_env() -> str:
    """Inspected the environment variables and credentials inside the container.
    
    Returns:
        A formatted string containing environment details.
    """
    res = f"Python Executable: {sys.executable}\n"
    
    # 1. Inspect google.auth.default()
    try:
        import google.auth
        credentials, project = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        res += f"google.auth.default() resolved project: {project}\n"
        res += f"Credentials Class: {credentials.__class__.__name__}\n"
        
        # Try refreshing
        from google.auth.transport.requests import Request
        credentials.refresh(Request())
        res += f"Token present: {bool(credentials.token)}\n"
        if credentials.token:
            res += f"Token starts with: {credentials.token[:15]}...\n"
    except Exception as auth_err:
        res += f"google.auth.default() failed: {auth_err}\n"
        
    # 2. Inspect google.genai Client
    try:
        from google.genai import Client
        from app.app_utils.env_resolver import get_project_id, get_region
        
        proj_resolved = get_project_id()
        reg_resolved = get_region()
        res += f"env_resolver resolved project: {proj_resolved}, region: {reg_resolved}\n"
        
        # Initialize Client
        client = Client(
            vertexai=True,
            project=proj_resolved,
            location=reg_resolved
        )
        
        # Inspect Client properties
        # Internal properties in google-genai Client might be client.api_key, client.credentials etc.
        res += "Client initialized successfully.\n"
        res += f"Client project: {getattr(client, '_project', None) or getattr(client, 'project', None)}\n"
        res += f"Client location: {getattr(client, '_location', None) or getattr(client, 'location', None)}\n"
        res += f"Client api_key: {bool(getattr(client, '_api_key', None) or getattr(client, 'api_key', None))}\n"
        
        # Try to make a model call
        try:
            model_resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents="Hello, say test."
            )
            res += f"Model Call SUCCESS: {model_resp.text}\n"
        except Exception as model_err:
            res += f"Model Call FAILED: {model_err}\n"
            import traceback
            res += f"Model Call Stack:\n{traceback.format_exc()}\n"
            
    except Exception as client_err:
        res += f"Client inspection failed: {client_err}\n"

    # 3. Environment variables
    env_vars = {k: v for k, v in os.environ.items() if not k.endswith("KEY") and "PASSWORD" not in k and "SECRET" not in k}
    res += f"Environment Variables:\n"
    for k, v in env_vars.items():
        res += f"  {k}: {v}\n"
        
    return res

