"""Percorsi locali dell'applicazione (configurazione, dati, log).

Su Windows:
  - configurazione: %APPDATA%\\backup-sys
  - dati (catalogo, audit, log): %LOCALAPPDATA%\\backup-sys
La variabile d'ambiente BACKUPSYS_HOME sovrascrive entrambi (usata dai test e per installazioni portable).
"""

from __future__ import annotations

import os
from pathlib import Path

from backupsys import APP_NAME


def _base(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    home = os.environ.get("BACKUPSYS_HOME")
    if home:
        p = Path(home) / "config"
    elif os.name == "nt":
        p = _base("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        p = _base("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    home = os.environ.get("BACKUPSYS_HOME")
    if home:
        p = Path(home) / "data"
    elif os.name == "nt":
        p = _base("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME
    else:
        p = _base("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
