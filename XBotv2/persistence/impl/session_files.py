"""Physical copying/deletion for the session lifecycle owner."""
from __future__ import annotations
import secrets
import shutil
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.errors import OperationError

def fork_persisted_session(paths: RuntimePaths, source_session_id: str) -> str:
    """Copy one persisted session to a fresh session id."""
    session_id = _new_fork_id()
    while paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = paths.session(source_session_id).root
    target = paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return session_id

def delete_persisted_session(paths: RuntimePaths, session_id: str) -> None:
    """Permanently remove one validated persisted session tree."""
    root = paths.session(session_id).root
    if root.is_symlink():
        raise OperationError(
            "invalid_session_storage",
            f"Cannot delete symlinked session storage: {session_id}",
        )
    shutil.rmtree(root)

def _new_fork_id() -> str:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"
