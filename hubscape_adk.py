import contextvars
import contextlib
from typing import Generator
from google.cloud import firestore

_current_context = contextvars.ContextVar("hubscape_context")

class RemoteAuth:
    def __init__(self, user_id: str):
        self.user_id = user_id
    
    def get_user_id(self) -> str:
        return self.user_id

class RemoteContext:
    def __init__(self, user_id: str, project_id: str = None, raw_context: dict = None):
        self.auth = RemoteAuth(user_id)
        self.project_id = project_id
        self.raw_context = raw_context or {}
        self.actions = []
        self._db = None

    @property
    def db(self):
        if self._db is None:
            # Under Vertex AI, ADC credentials will be picked up automatically
            # Explicitly specifying the project ID ensures correct database binding
            self._db = firestore.Client(project=self.project_id)
        return self._db

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
