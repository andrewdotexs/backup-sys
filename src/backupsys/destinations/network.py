"""Connessione a condivisioni di rete Windows (UNC) con credenziali dal Credential Manager.

Usa ``WNetAddConnection2W`` via ctypes: la password non passa mai dalla riga di comando
(come accadrebbe con ``net use``) e quindi non è visibile nell'elenco dei processi.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from backupsys.security import secrets

if TYPE_CHECKING:
    from backupsys.config.models import Destination


def unc_share(path: str) -> str | None:
    """``\\\\NAS\\backup\\pc`` -> ``\\\\NAS\\backup``."""
    if not path.startswith("\\\\"):
        return None
    parts = path[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"\\\\{parts[0]}\\{parts[1]}"


def ensure_connected(dest: Destination) -> None:
    if os.name != "nt" or os.path.isdir(dest.path):
        return
    share = unc_share(dest.path)
    if share is None or not dest.network_username:
        return
    password = secrets.get_secret(secrets.network_password_name(dest.id))
    if password is None:
        return
    _wnet_add_connection(share, dest.network_username, password)


def _wnet_add_connection(remote: str, user: str, password: str) -> None:  # pragma: no cover - solo Windows
    import ctypes
    from ctypes import wintypes

    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    RESOURCETYPE_DISK = 1
    nr = NETRESOURCEW()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpRemoteName = remote
    mpr = ctypes.WinDLL("mpr")  # type: ignore[attr-defined]
    rc = mpr.WNetAddConnection2W(ctypes.byref(nr), password, user, 0)
    if rc not in (0, 1219):  # 1219 = già connesso con altre credenziali
        raise OSError(rc, f"Connessione a {remote} non riuscita (codice {rc})")
