"""Accesso ai segreti tramite il keyring del sistema operativo (Windows Credential Manager).

Le password di rete e le chiavi di cifratura non finiscono mai nel file di configurazione.
"""

from __future__ import annotations

import base64
import contextlib
import os
import threading

SERVICE = "backup-sys"

#: ``BACKUPSYS_KEYRING=memory`` usa un keyring in memoria (SOLO sviluppo/test: i segreti si perdono alla chiusura).
_memory: dict[str, str] | None = {} if os.environ.get("BACKUPSYS_KEYRING") == "memory" else None
_lock = threading.Lock()


def use_memory_backend() -> None:
    """Backend in memoria (test, o ambienti senza keyring)."""
    global _memory
    with _lock:
        _memory = {}


def set_secret(name: str, value: str) -> None:
    with _lock:
        if _memory is not None:
            _memory[name] = value
            return
    import keyring

    keyring.set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    with _lock:
        if _memory is not None:
            return _memory.get(name)
    import keyring

    return keyring.get_password(SERVICE, name)


def forget_secret(name: str) -> None:
    with _lock:
        if _memory is not None:
            _memory.pop(name, None)
            return
    import keyring
    from keyring.errors import PasswordDeleteError

    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(SERVICE, name)


def network_password_name(dest_id: str) -> str:
    return f"net-password:{dest_id}"


def dest_key_name(dest_id: str) -> str:
    return f"dest-key:{dest_id}"


def set_dest_key(dest_id: str, key: bytes) -> None:
    set_secret(dest_key_name(dest_id), base64.b64encode(key).decode())


def get_dest_key(dest_id: str) -> bytes | None:
    v = get_secret(dest_key_name(dest_id))
    return base64.b64decode(v) if v else None
