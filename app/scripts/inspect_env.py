import os
import sys

def inspect_env() -> str:
    """Inspected the environment variables and credentials inside the container.
    
    Returns:
        A formatted string containing environment details.
    """
    try:
        import google.auth
        credentials, project = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        # Try to refresh/get token
        from google.auth.transport.requests import Request
        credentials.refresh(Request())
        token_info = f"Token present: {bool(credentials.token)}"
        if credentials.token:
            token_info += f" (length: {len(credentials.token)}, starts with: {credentials.token[:10]})"
    except Exception as e:
        token_info = f"Failed to load credentials: {e}"

    env_vars = {k: v for k, v in os.environ.items() if not k.endswith("KEY") and "PASSWORD" not in k and "SECRET" not in k}
    
    res = f"Python Executable: {sys.executable}\n"
    res += f"Python Path: {sys.path}\n"
    res += f"Project: {project}\n"
    res += f"Token Info: {token_info}\n"
    res += f"Environment Variables:\n"
    for k, v in env_vars.items():
        res += f"  {k}: {v}\n"
    return res
