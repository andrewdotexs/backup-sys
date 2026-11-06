"""Audit log solo-aggiunta con catena HMAC.

Ogni riga JSON contiene l'HMAC-SHA256 di (hmac precedente || contenuto). La chiave HMAC è nel
Credential Manager: chi modifica, cancella o riordina righe del file senza la chiave rompe la catena,
e ``verify()`` lo rileva.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as pysecrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backupsys import appdirs
from backupsys.security import secrets

GENESIS = "0" * 64
KEY_NAME = "audit-hmac-key"


def _key() -> bytes:
    k = secrets.get_secret(KEY_NAME)
    if k is None:
        k = pysecrets.token_hex(32)
        secrets.set_secret(KEY_NAME, k)
    return bytes.fromhex(k)


def _canonical(rec: dict[str, Any]) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    broken_at: int | None = None  # numero di riga (1-based)
    message: str = ""


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or appdirs.data_dir() / "audit.log"
        self._lock = threading.Lock()

    def _last_mac(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open("rb") as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                last = json.loads(line)["mac"]
                break
            except (ValueError, KeyError):
                continue
        return last

    def log(self, action: str, actor: str = "system", **details: Any) -> None:
        with self._lock:
            prev = self._last_mac()
            rec: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "actor": actor,
                "action": action,
                "details": details,
                "prev": prev,
            }
            rec["mac"] = hmac.new(_key(), prev.encode() + _canonical(rec), hashlib.sha256).hexdigest()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def entries(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return list(reversed(out))[:limit]

    def verify(self) -> VerifyResult:
        if not self.path.exists():
            return VerifyResult(True, 0, message="Registro vuoto")
        key = _key()
        prev = GENESIS
        n = 0
        with self.path.open(encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                try:
                    rec = json.loads(line)
                    mac = rec.pop("mac")
                except (ValueError, KeyError):
                    return VerifyResult(False, n - 1, n, f"Riga {n} illeggibile")
                if rec.get("prev") != prev:
                    return VerifyResult(False, n - 1, n, f"Catena interrotta alla riga {n} (righe rimosse?)")
                expected = hmac.new(key, prev.encode() + _canonical(rec), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, expected):
                    return VerifyResult(False, n - 1, n, f"Riga {n} modificata")
                prev = mac
        return VerifyResult(True, n, message=f"Integro ({n} voci)")
