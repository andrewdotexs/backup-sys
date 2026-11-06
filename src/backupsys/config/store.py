"""Lettura/scrittura atomica della configurazione TOML."""

from __future__ import annotations

import os
import threading
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from backupsys import appdirs
from backupsys.config.models import AppConfig


def _strip_none(obj: Any) -> Any:
    """TOML non supporta ``null``: si omettono le chiavi a ``None``."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or appdirs.config_dir() / "config.toml"
        self._lock = threading.RLock()

    def load(self) -> AppConfig:
        with self._lock:
            if not self.path.exists():
                return AppConfig()
            with self.path.open("rb") as f:
                return AppConfig.model_validate(tomllib.load(f))

    def save(self, config: AppConfig) -> None:
        with self._lock:
            data = _strip_none(config.model_dump(mode="json"))
            tmp = self.path.with_suffix(".toml.tmp")
            with tmp.open("wb") as f:
                tomli_w.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # file di configurazione locale: la sostituzione è voluta
