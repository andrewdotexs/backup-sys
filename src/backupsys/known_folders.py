"""Cartelle note dell'utente (Documenti, Desktop, ...) con il loro percorso REALE.

Su Windows si usa ``SHGetKnownFolderPath``: tiene conto della localizzazione (la cartella
"Documenti" è in realtà ``Documents``) e del reindirizzamento su OneDrive.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

#: (etichetta, FOLDERID, nome di ripiego sotto la home)
FOLDERS = [
    ("Documenti", "FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Documents"),
    ("Desktop", "B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
    ("Immagini", "33E28130-4E1E-4676-835A-98395C3BC3BB", "Pictures"),
    ("Musica", "4BD8D571-6D19-48D3-BE97-422220080E43", "Music"),
    ("Video", "18989B1D-99B5-455B-841C-AB7C74E4DDFC", "Videos"),
    ("Download", "374DE290-123F-4565-9164-39C4925E467B", "Downloads"),
]


def _known_folder_windows(guid: str) -> str | None:  # pragma: no cover - solo Windows
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    u = uuid.UUID(guid)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    rc = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    try:
        return out.value if rc == 0 else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def known_folders() -> list[tuple[str, str]]:
    """[(etichetta, percorso reale)] delle cartelle esistenti."""
    out = []
    for label, guid, fallback in FOLDERS:
        path = _known_folder_windows(guid) if os.name == "nt" else None
        path = path or str(Path.home() / fallback)
        if os.path.isdir(path):
            out.append((label, path))
    return out
