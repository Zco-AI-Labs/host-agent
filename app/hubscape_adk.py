import contextvars
import contextlib
import datetime
from typing import Generator, Optional
from google.cloud import firestore

_current_context = contextvars.ContextVar("hubscape_context")
_global_active_context = None

class RemoteAuth:
    def __init__(self, user_id: str, org_id: str = None, hub_id: str = None):
        self.user_id = user_id
        self.org_id = org_id
        self.hub_id = hub_id
    
    def get_user_id(self) -> str:
        return self.user_id

class RemoteContext:
    def __init__(self, user_id: str, agent_id: str = None, org_id: str = None, hub_id: str = None, project_id: str = None, raw_context: dict = None, allow_generative_ui: Optional[bool] = None):
        self.auth = RemoteAuth(user_id, org_id, hub_id)
        self.agent_id = agent_id or "default_agent"
        self.project_id = project_id
        self.raw_context = raw_context or {}
        self.actions = []
        self._db = None
        
        # Resolve allow_generative_ui flag
        if allow_generative_ui is not None:
            self.allow_generative_ui = allow_generative_ui
        else:
            platform_config = self.raw_context.get("config") or {}
            self.allow_generative_ui = platform_config.get("allowGenerativeUi", True)

    @property
    def _db_client(self):
        if self._db is None:
            # Try to get OAuth2 token from Metadata Server
            token = None
            try:
                import httpx as httpx_sync
                meta_url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
                resp = httpx_sync.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=2.0)
                if resp.status_code == 200:
                    token = resp.json().get("access_token")
            except Exception:
                pass

            if token:
                from google.oauth2.credentials import Credentials as OAuth2Credentials
                creds = OAuth2Credentials(token)
                self._db = firestore.Client(project=self.project_id, credentials=creds)
            else:
                self._db = firestore.Client(project=self.project_id)
        return self._db

    def get_agent_db_path(self, scope: str, collection_name: str, doc_id: Optional[str] = None) -> str:
        if scope == "user":
            base = f"platform_users/{self.auth.get_user_id()}/agent_data/{self.agent_id}/{collection_name}"
        elif scope == "hub":
            if not self.auth.hub_id or not self.auth.org_id:
                raise ValueError("Hub scope requires org_id and hub_id in context.")
            base = f"organizations/{self.auth.org_id}/hubs/{self.auth.hub_id}/agent_data/{self.agent_id}/{collection_name}"
        elif scope == "org":
            if not self.auth.org_id:
                raise ValueError("Org scope requires org_id in context.")
            base = f"organizations/{self.auth.org_id}/agent_data/{self.agent_id}/{collection_name}"
        else:
            raise ValueError(f"Unknown scope: {scope}")
            
        if doc_id:
            return f"{base}/{doc_id}"
        return base

    def save(self, scope: str, collection_name: str, doc_id: str, data: dict) -> dict:
        doc_path = self.get_agent_db_path(scope, collection_name, doc_id)
        doc_ref = self._db_client.document(doc_path)
        
        snap = doc_ref.get()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        user_id = self.auth.get_user_id()
        
        payload = data.copy()
        if not snap.exists:
            payload.update({
                "created_at": now,
                "created_by": user_id,
                "updated_at": now,
                "updated_by": user_id,
                "version": 1
            })
        else:
            current_data = snap.to_dict() or {}
            current_version = current_data.get("version", 0)
            payload.update({
                "created_at": current_data.get("created_at", now),
                "created_by": current_data.get("created_by", user_id),
                "updated_at": now,
                "updated_by": user_id,
                "version": current_version + 1
            })
            
        doc_ref.set(payload, merge=True)
        return payload

    def get(self, scope: str, collection_name: str, doc_id: str) -> Optional[dict]:
        doc_path = self.get_agent_db_path(scope, collection_name, doc_id)
        doc_ref = self._db_client.document(doc_path)
        snap = doc_ref.get()
        if snap.exists:
            res = snap.to_dict() or {}
            res["id"] = snap.id
            return res
        return None

    def list(self, scope: str, collection_name: str) -> list:
        col_path = self.get_agent_db_path(scope, collection_name)
        col_ref = self._db_client.collection(col_path)
        docs = col_ref.stream()
        res = []
        for doc in docs:
            d = doc.to_dict() or {}
            d["id"] = doc.id
            res.append(d)
        return res

    def delete(self, scope: str, collection_name: str, doc_id: str):
        doc_path = self.get_agent_db_path(scope, collection_name, doc_id)
        doc_ref = self._db_client.document(doc_path)
        doc_ref.delete()

    def show_widget(self, widget_template_id: str, data: dict = None) -> dict:
        """Loads a predefined widget JSON and registers a client action directive to show it."""
        try:
            import os
            import json
            runtime_dir = os.path.dirname(os.path.abspath(__file__))
            filename = widget_template_id if widget_template_id.endswith(".json") else f"{widget_template_id}.json"
            template_path = os.path.join(runtime_dir, "widgets", filename)
            if not os.path.exists(template_path):
                raise FileNotFoundError(f"Widget template {filename} not found at: {template_path}")
            
            with open(template_path, "r", encoding="utf-8") as f:
                widget_config = json.load(f)

            # Replacements (e.g. {{agent_id}} -> actual agent ID)
            config_str = json.dumps(widget_config).replace("{{agent_id}}", self.agent_id)
            widget_config = json.loads(config_str)

            action_payload = {
                "type": "OPEN_AGENT_WIDGET",
                "payload": {
                    "widgetId": widget_template_id,
                    "widgetConfig": widget_config,
                    "data": data or {},
                    "styling": self.raw_context.get("styling", {}),
                    "userPreferences": self.raw_context.get("userPreferences", {})
                }
            }
            self.actions.append(action_payload)
            return {"status": "success", "message": f"Widget '{widget_template_id}' queued."}
        except Exception as e:
            raise RuntimeError(f"Failed to load widget '{widget_template_id}': {str(e)}")

    def show_custom_ui(self, layout: dict, data: dict = None) -> dict:
        """Registers an OPEN_AGENT_WIDGET client action directive with a generative layout."""
        if not getattr(self, "allow_generative_ui", True):
            raise PermissionError("Generative UI is disabled for this agent. Only predefined developer widgets are allowed.")
            
        action_payload = {
            "type": "OPEN_AGENT_WIDGET",
            "payload": {
                "widgetId": "generative_custom_ui",
                "widgetConfig": layout,
                "data": data or {},
                "styling": self.raw_context.get("styling", {}),
                "userPreferences": self.raw_context.get("userPreferences", {})
            }
        }
        self.actions.append(action_payload)
        return {"status": "success", "message": "Custom UI layout queued."}

def get_context() -> RemoteContext:
    try:
        return _current_context.get()
    except LookupError:
        global _global_active_context
        if _global_active_context is not None:
            return _global_active_context
        raise RuntimeError(
            "No active RemoteContext found. "
            "Ensure the tool is executed inside an active context_session."
        )

@contextlib.contextmanager
def context_session(context: RemoteContext) -> Generator[None, None, None]:
    global _global_active_context
    old_global = _global_active_context
    _global_active_context = context
    token = _current_context.set(context)
    try:
        yield
    finally:
        _global_active_context = old_global
        try:
            _current_context.reset(token)
        except ValueError:
            pass

import functools
import inspect
import os
import json
import logging
import jwt
from cryptography.fernet import Fernet

def require_tool_privilege(func):
    """
    Decorator for agent python tools to enforce zero-trust tool-level RBAC.
    Supports both synchronous and asynchronous functions.
    """
    is_async = inspect.iscoroutinefunction(func)

    def verify_privilege():
        context = get_context()
        
        # Check Developer Bypass
        secret_key = os.environ.get("HUBSCAPE_HMAC_SECRET")
        if not secret_key:
            logging.getLogger(__name__).warning(
                f"⚠️ Developer Bypass Active: Tool '{func.__name__}' executed without JWT check."
            )
            return True
            
        token = context.raw_context.get("capability_token")
        if not token:
            raise PermissionError(f"Security Block: Access denied to tool '{func.__name__}'. Missing capability token.")
            
        try:
            # Decode & Verify JWT HMAC
            payload = jwt.decode(token, secret_key, algorithms=["HS256"])
            
            # Context Pinning Checks
            token_user_id = payload.get("sub")
            metadata_user_id = context.auth.get_user_id()
            if token_user_id != metadata_user_id:
                raise PermissionError("Security Block: User ID mismatch between request and capability token.")
                
            token_hub_id = payload.get("hub_id")
            metadata_hub_id = context.auth.hub_id
            if token_hub_id != metadata_hub_id:
                raise PermissionError("Security Block: Hub ID mismatch between request and capability token.")
                
            # Derive Fernet key dynamically from master secret
            import base64
            import hashlib
            hasher = hashlib.sha256()
            hasher.update(secret_key.encode())
            hasher.update(context.agent_id.encode())
            derived_key = base64.urlsafe_b64encode(hasher.digest()).decode()
            
            encrypted_capabilities = payload.get("capabilities", {})
            encrypted_segment = encrypted_capabilities.get(context.agent_id)
            
            if not encrypted_segment:
                raise PermissionError(f"Security Block: Agent '{context.agent_id}' is not authorized in this session passport.")
                
            try:
                fernet = Fernet(derived_key.encode())
                decrypted_bytes = fernet.decrypt(encrypted_segment.encode())
                allowed_tools = json.loads(decrypted_bytes.decode())
            except Exception as decrypt_err:
                raise PermissionError(f"Security Block: Failed to decrypt capabilities: {decrypt_err}")
                
            if func.__name__ not in allowed_tools:
                raise PermissionError(f"Security Block: Tool '{func.__name__}' is not allowed for this agent. Allowed: {allowed_tools}")
                
            return True
            
        except jwt.ExpiredSignatureError:
            raise PermissionError("Security Block: Capability token has expired.")
        except jwt.InvalidTokenError as jwt_err:
            raise PermissionError(f"Security Block: Invalid capability token: {jwt_err}")

    if is_async:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            verify_privilege()
            return await func(*args, **kwargs)
        return async_wrapper
    else:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            verify_privilege()
            return func(*args, **kwargs)
        return sync_wrapper
