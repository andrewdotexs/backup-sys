"""Manifest per-esecuzione (JSON Lines) salvato sul disco di backup.

Ogni esecuzione scrive ``.bsys/manifests/<run_ts>.jsonl`` con:
  {"op": "store",   "p": "current/...", "sha256": "...", "size": n, "mtime_ns": n}
  {"op": "archive", "from": "current/...", "to": "versions/<ts>/..."}
Rileggendo tutti i manifest in ordine si ottiene l'hash atteso di ogni file (per la verifica di integrità).
Formato a righe, solo-aggiunta: robusto anche su condivisioni di rete (dove SQLite non lo è).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from backupsys.core.fsutil import long_path


class ManifestWriter:
    def __init__(self, manifests_dir: Path, run_ts: str) -> None:
        self.path = manifests_dir / f"{run_ts}.jsonl"
        self._f: IO[str] | None = None
        self._dir = manifests_dir

    def _write(self, rec: dict[str, Any]) -> None:
        if self._f is None:
            os.makedirs(long_path(self._dir), exist_ok=True)
            self._f = open(long_path(self.path), "a", encoding="utf-8")  # noqa: SIM115
        self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def stored(self, rel: str, sha256: str, size: int, mtime_ns: int) -> None:
        self._write({"op": "store", "p": rel, "sha256": sha256, "size": size, "mtime_ns": mtime_ns})

    def archived(self, rel_from: str, rel_to: str) -> None:
        self._write({"op": "archive", "from": rel_from, "to": rel_to})

    def renamed(self, rel_from: str, rel_to: str) -> None:
        """``rel_to`` è lo stesso file di ``rel_from`` rinominato/spostato sul PC (collegati con hard link)."""
        self._write({"op": "rename", "from": rel_from, "to": rel_to})

    def close(self) -> None:
        if self._f is not None:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()
            self._f = None


@dataclass(frozen=True)
class Expected:
    sha256: str
    size: int


def load_expected(manifests_dir: Path) -> dict[str, Expected]:
    """Mappa percorso-relativo-al-job → hash atteso, ricostruita da tutti i manifest."""
    state: dict[str, Expected] = {}
    if not manifests_dir.is_dir():
        return state
    for mf in sorted(manifests_dir.glob("*.jsonl")):
        with open(long_path(mf), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # riga troncata da un'interruzione
                if rec.get("op") == "store":
                    state[rec["p"]] = Expected(rec["sha256"], rec["size"])
                elif rec.get("op") == "archive":
                    src, dst = rec["from"], rec["to"]
                    prefix = src + "/"
                    for k in [k for k in state if k == src or k.startswith(prefix)]:
                        state[dst + k[len(src) :]] = state.pop(k)
    return state


def load_renames(manifests_dir: Path) -> dict[str, str]:
    """Rinomine riconosciute: percorso nuovo (``current/...``) -> percorso precedente (``current/...``)."""
    out: dict[str, str] = {}
    if not manifests_dir.is_dir():
        return out
    for mf in sorted(manifests_dir.glob("*.jsonl")):
        with open(long_path(mf), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("op") == "rename":
                    out[rec["to"]] = rec["from"]
    return out
