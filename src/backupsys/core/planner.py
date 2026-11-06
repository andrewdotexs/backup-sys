"""Confronto sorgente ↔ destinazione **reale** e piano delle azioni.

Il confronto viene fatto sempre contro i file effettivamente presenti sul disco di backup
(non contro un database): se l'utente cancella un file dal backup, qui risulta mancante
e viene ricopiato.
"""

from __future__ import annotations

import enum
import hashlib
import os
from dataclasses import dataclass, field

from backupsys.core.fsutil import long_path, mtimes_equal
from backupsys.core.layout import JobLayout
from backupsys.core.scanner import SourceFile
from backupsys.security import crypto


class Action(enum.Enum):
    COPY = "copy"  # assente sul backup
    NEW_VERSION = "new_version"  # presente ma diverso: la vecchia va in versions/
    SKIP = "skip"  # identico


@dataclass(frozen=True, slots=True)
class PlannedItem:
    src: SourceFile
    action: Action
    dest_path: str


@dataclass
class Plan:
    items: list[PlannedItem] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def count(self, action: Action) -> int:
        return sum(1 for i in self.items if i.action is action)

    @property
    def to_transfer(self) -> list[PlannedItem]:
        return [i for i in self.items if i.action is not Action.SKIP]

    @property
    def bytes_to_transfer(self) -> int:
        return sum(i.src.size for i in self.to_transfer)

    @property
    def existing_count(self) -> int:
        return sum(1 for i in self.items if i.action is not Action.COPY)


def dest_path_for(layout: JobLayout, src: SourceFile, encrypted: bool) -> str:
    p = os.path.join(layout.current, *src.backup_rel.split("/"))
    return p + crypto.SUFFIX if encrypted else p


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dest_sha256(path: str, cipher: crypto.StreamCipher | None) -> str:
    if cipher is None:
        return sha256_file(path)
    with open(long_path(path), "rb") as f:
        return cipher.decrypt_stream(f, None)[0]


def decide(src: SourceFile, dest: str, cipher: crypto.StreamCipher | None, encrypted: bool, deep: bool) -> Action:
    try:
        st = os.stat(long_path(dest))
    except FileNotFoundError:
        return Action.COPY
    except NotADirectoryError:
        return Action.COPY  # un antenato è un file: SafeWriter lo sposterà in versions/
    if not os.path.isfile(long_path(dest)):
        return Action.NEW_VERSION  # al posto del file c'è una cartella
    expected = crypto.encrypted_size(src.size) if encrypted else src.size
    if st.st_size != expected or not mtimes_equal(st.st_mtime_ns, src.mtime_ns):
        return Action.NEW_VERSION
    if deep:
        try:
            if sha256_file(src.abs_path) != _dest_sha256(dest, cipher):
                return Action.NEW_VERSION
        except crypto.DecryptionError:
            return Action.NEW_VERSION
    return Action.SKIP


def build_plan(
    files: list[SourceFile],
    layout: JobLayout,
    encrypted: bool = False,
    cipher: crypto.StreamCipher | None = None,
    deep: bool = False,
) -> Plan:
    plan = Plan()
    for src in files:
        dest = dest_path_for(layout, src, encrypted)
        try:
            action = decide(src, dest, cipher, encrypted, deep)
        except OSError as e:
            plan.errors.append((src.abs_path, f"Confronto non riuscito: {e.strerror or e}"))
            continue
        plan.items.append(PlannedItem(src, action, dest))
    return plan
