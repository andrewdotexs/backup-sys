"""Destinazioni: dischi locali/USB e percorsi di rete (UNC / unità mappate).

Ogni disco viene riconosciuto dal marker ``backup-sys/.bsys-volume.json`` e non dalla lettera
di unità: se al posto di E: c'è un disco diverso il backup su quella destinazione non parte.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupsys.config.models import Destination
from backupsys.core.layout import DestLayout
from backupsys.destinations import network
from backupsys.security import crypto, secrets


@dataclass(frozen=True)
class DestStatus:
    available: bool
    identity_ok: bool
    message: str
    volume_id: str | None = None

    @property
    def ready(self) -> bool:
        return self.available and self.identity_ok


def read_marker(layout: DestLayout) -> dict[str, str] | None:
    try:
        data = json.loads(layout.marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "volume_id" in data else None


def write_marker(layout: DestLayout, dest: Destination) -> str:
    vid = uuid.uuid4().hex
    layout.root.mkdir(parents=True, exist_ok=True)
    doc = {"volume_id": vid, "name": dest.name, "created_at": datetime.now().isoformat(timespec="seconds")}
    with open(layout.marker, "x", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return vid


def is_reachable(dest: Destination) -> bool:
    if dest.kind == "network":
        network.ensure_connected(dest)
    return os.path.isdir(dest.path)


def check(dest: Destination) -> DestStatus:
    """Stato della destinazione senza modificare nulla."""
    try:
        reachable = is_reachable(dest)
    except OSError as e:
        return DestStatus(False, False, f"Non raggiungibile: {e}")
    if not reachable:
        what = "Percorso di rete non raggiungibile" if dest.kind == "network" else "Disco non collegato"
        return DestStatus(False, False, what)
    layout = DestLayout(Path(dest.path))
    marker = read_marker(layout)
    if dest.volume_id is None:
        if marker:
            return DestStatus(True, True, "Disco con backup esistente, verrà associato", marker["volume_id"])
        return DestStatus(True, True, "Disco nuovo, verrà inizializzato")
    if marker is None:
        return DestStatus(
            True, False, "Disco non riconosciuto: manca il marker backup-sys (disco diverso o cartella rimossa)"
        )
    if marker["volume_id"] != dest.volume_id:
        return DestStatus(True, False, "Disco diverso da quello associato a questa destinazione")
    return DestStatus(True, True, "Pronto", dest.volume_id)


def bind(dest: Destination) -> str:
    """Inizializza o associa il disco. Restituisce il volume_id da salvare in configurazione."""
    layout = DestLayout(Path(dest.path))
    marker = read_marker(layout)
    if marker:
        return marker["volume_id"]
    return write_marker(layout, dest)


def load_cipher(dest: Destination) -> crypto.StreamCipher | None:
    if not dest.encrypted:
        return None
    key = secrets.get_dest_key(dest.id)
    if key is None:
        raise PermissionError("Chiave di cifratura non disponibile: sblocca la destinazione dall'interfaccia")
    keyfile = DestLayout(Path(dest.path)).root / crypto.KEYFILE_NAME
    if keyfile.exists() and not crypto.key_matches_keyfile(keyfile, key):
        raise PermissionError("La chiave salvata non corrisponde a quella del disco")
    return crypto.StreamCipher(key)
