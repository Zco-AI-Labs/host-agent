import contextvars
import contextlib
import datetime
from typing import Generator, Optional
from google.cloud import firestore

_current_context = contextvars.ContextVar("hubscape_context")

class RemoteAuth:
    def __init__(self, user_id: str, org_id: str = None, hub_id: str = None):
        self.user_id = user_id
        self.org_id = org_id
        self.hub_id = hub_id
    
    def get_user_id(self) -> str:
        return self.user_id

class RemoteContext:
    def __init__(self, user_id: str, agent_id: str = None, org_id: str = None, hub_id: str = None, project_id: str = None, raw_context: dict = None):
        self.auth = RemoteAuth(user_id, org_id, hub_id)
        self.agent_id = agent_id or "default_agent"
        self.project_id = project_id
        self.raw_context = raw_context or {}
        self.actions = []
        self._db = None

    @property
    def _db_client(self):
        if self._db is None:
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

def get_context() -> RemoteContext:
    try:
        return _current_context.get()
    except LookupError:
        raise RuntimeError(
            "No active RemoteContext found. "
            "Ensure the tool is executed inside an active context_session."
        )

@contextlib.contextmanager
def context_session(context: RemoteContext) -> Generator[None, None, None]:
    token = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)
