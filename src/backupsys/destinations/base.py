"""Destinazioni: dischi locali/USB e percorsi di rete (UNC / unità mappate).

Ogni disco viene riconosciuto dal marker ``backup-sys/.bsys-volume.json`` e non dalla lettera
di unità: se al posto di E: c'è un disco diverso il backup su quella destinazione non parte.

Un disco può diventare un disco di backup solo se è **vuoto** (appena formattato): per i dischi locali
si controlla l'intero volume, ignorando soltanto le cartelle di sistema di Windows; per i percorsi di rete
la cartella di destinazione. Il controllo vale solo per l'inizializzazione: un disco già inizializzato
(marker presente) viene riconosciuto senza ripetere il controllo.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backupsys.config.models import Destination
from backupsys.core.layout import ROOT_DIR, DestLayout
from backupsys.destinations import network
from backupsys.security import crypto, secrets


@dataclass(frozen=True)
class DestStatus:
    available: bool
    identity_ok: bool
    message: str
    volume_id: str | None = None
    #: percorso dove il disco è stato ritrovato, se diverso da quello configurato (cambio di lettera)
    relocated_to: str | None = None

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


#: Voci create da Windows su ogni volume: non contano come "contenuto" del disco.
SYSTEM_ENTRIES = frozenset({"system volume information", "$recycle.bin", "recycler", "desktop.ini", "thumbs.db"})
MAX_LISTED = 5


class DiskNotEmptyError(ValueError):
    pass


def volume_root(path: str) -> Path:
    """Radice del volume che contiene ``path`` (``E:\\`` su Windows, punto di mount su POSIX)."""
    p = Path(os.path.abspath(path))
    if os.name == "nt":
        drive = os.path.splitdrive(str(p))[0]
        return Path(drive + "\\") if drive else p
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return p


def _entries(d: Path) -> list[str]:
    try:
        return [e for e in os.listdir(d) if e.lower() not in SYSTEM_ENTRIES]
    except OSError:
        return []


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower() if os.name == "nt" else a == b


def foreign_entries(dest: Destination) -> list[str]:
    """Contenuti estranei che impediscono di usare la destinazione come disco di backup.

    Dischi locali: dalla radice del volume fino alla cartella di destinazione ogni livello può contenere
    solo la cartella successiva del percorso; la destinazione può contenere solo ``backup-sys``.
    Percorsi di rete: solo la cartella di destinazione viene controllata.
    """
    target = Path(os.path.abspath(dest.path))
    parts: tuple[str, ...]
    if dest.kind == "network":
        root, parts = target, ()
    else:
        root = volume_root(dest.path)
        try:
            parts = target.relative_to(root).parts
        except ValueError:
            root, parts = target, ()
    found: list[str] = []
    cur = root
    for nxt in [*parts, None]:
        if not cur.is_dir():
            break  # da qui in giù le cartelle non esistono ancora: verranno create
        allowed = nxt if nxt is not None else ROOT_DIR
        found += [str(cur / e) for e in _entries(cur) if not _same(e, allowed)]
        if nxt is None:
            break
        cur = cur / nxt
    return found


def _not_empty_message(found: list[str]) -> str:
    shown = ", ".join(Path(f).name for f in found[:MAX_LISTED])
    more = f" e altri {len(found) - MAX_LISTED}" if len(found) > MAX_LISTED else ""
    return f"Il disco non è vuoto ({shown}{more}): per sicurezza va formattato (NTFS) prima di usarlo come backup"


def _container_available(dest: Destination) -> bool:
    """La cartella di destinazione non esiste ancora, ma il disco/la condivisione che la conterrà sì."""
    if dest.kind == "network":
        share = network.unc_share(dest.path)
        return bool(share and os.path.isdir(share))
    root = volume_root(dest.path)
    return root != Path(os.path.abspath(dest.path)) and root.is_dir()


def is_reachable(dest: Destination) -> bool:
    if dest.kind == "network":
        network.ensure_connected(dest)
    return os.path.isdir(dest.path)


def check(dest: Destination) -> DestStatus:
    """Stato della destinazione senza modificare nulla.

    Un disco locale già associato che non si trova alla sua lettera (o al cui posto c'è un altro disco)
    viene cercato sulle altre unità tramite il suo identificativo: ``relocated_to`` indica dove.
    """
    status = _check_at(dest)
    if status.ready or dest.kind != "local" or dest.volume_id is None:
        return status
    found = locate(dest)
    if found is None:
        return status
    moved = _check_at(dest.model_copy(update={"path": found}))
    if not moved.ready:
        return status
    return DestStatus(
        True, True, f"Ritrovato su {found} (configurato su {dest.path})", dest.volume_id, relocated_to=found
    )


def _check_at(dest: Destination) -> DestStatus:
    try:
        reachable = is_reachable(dest)
    except OSError as e:
        return DestStatus(False, False, f"Non raggiungibile: {e}")
    if not reachable and not (dest.volume_id is None and _container_available(dest)):
        what = "Percorso di rete non raggiungibile" if dest.kind == "network" else "Disco non collegato"
        return DestStatus(False, False, what)
    layout = DestLayout(Path(dest.path))
    marker = read_marker(layout) if reachable else None
    if dest.volume_id is None:
        if marker:
            return DestStatus(True, True, "Disco con backup esistente, verrà associato", marker["volume_id"])
        found = foreign_entries(dest)
        if found:
            return DestStatus(True, False, _not_empty_message(found))
        return DestStatus(True, True, "Disco vuoto, verrà inizializzato al primo backup")
    if marker is None:
        return DestStatus(
            True, False, "Disco non riconosciuto: manca il marker backup-sys (disco diverso o cartella rimossa)"
        )
    if marker["volume_id"] != dest.volume_id:
        return DestStatus(True, False, "Disco diverso da quello associato a questa destinazione")
    return DestStatus(True, True, "Pronto", dest.volume_id)


DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_RAMDISK = 2, 3, 6


def candidate_volumes() -> list[Path]:
    """Unità in cui cercare un disco che ha cambiato lettera.

    Windows: dischi fissi e rimovibili (non lettori ottici né unità di rete: eviterebbero attese e
    finestre «Inserire un disco»). Altri sistemi: nessuna (la funzione è sostituibile nei test).
    """
    if sys.platform != "win32":
        return []
    import ctypes  # pragma: no cover - solo Windows

    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(0x8001)  # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX: niente finestre di errore
    mask = k32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(root) in (DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_RAMDISK):
                out.append(Path(root))
    return out


def locate(dest: Destination) -> str | None:
    """Cerca il disco associato a ``dest`` (stesso identificativo) sulle altre unità, nello stesso percorso
    relativo: ``F:\\Backup\\PC`` -> ``G:\\Backup\\PC``."""
    if dest.volume_id is None:
        return None
    target = Path(os.path.abspath(dest.path))
    root = volume_root(dest.path)
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None
    for vol in candidate_volumes():
        cand = vol / rel
        if os.path.normcase(str(cand)) == os.path.normcase(str(target)):
            continue
        try:
            marker = read_marker(DestLayout(cand))
        except OSError:
            continue
        if marker and marker.get("volume_id") == dest.volume_id:
            return str(cand)
    return None


def bind(dest: Destination) -> str:
    """Inizializza o associa il disco. Restituisce il volume_id da salvare in configurazione.

    Un disco senza marker viene inizializzato solo se è vuoto (``DiskNotEmptyError`` altrimenti);
    la cartella di destinazione viene creata se manca.
    """
    layout = DestLayout(Path(dest.path))
    marker = read_marker(layout)
    if marker:
        return marker["volume_id"]
    found = foreign_entries(dest)
    if found:
        raise DiskNotEmptyError(_not_empty_message(found))
    os.makedirs(dest.path, exist_ok=True)
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
