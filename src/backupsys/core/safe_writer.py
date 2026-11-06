"""SafeWriter: l'UNICO componente che scrive sui dischi di backup.

Garanzie:
- non espone alcuna operazione di cancellazione;
- non sovrascrive mai un file: una versione esistente viene prima *spostata* in ``versions/<run>/``;
- scrittura atomica: si scrive in ``<nome>.bsys-partial`` e si rinomina solo a copia completa e
  sincronizzata su disco (``fsync``). Uno stacco del disco lascia al più un ``.bsys-partial``,
  mai un file troncato con il nome definitivo.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from backupsys.core.fsutil import is_relative_to, long_path, rename_no_clobber
from backupsys.core.layout import PARTIAL_SUFFIX, JobLayout
from backupsys.core.manifest import ManifestWriter
from backupsys.core.planner import Action, PlannedItem
from backupsys.security import crypto

BUF = 1024 * 1024


class SourceChangedError(Exception):
    """Il file sorgente è cambiato durante la copia (verrà ricopiato alla prossima esecuzione)."""


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    size: int
    archived_to: str | None


class SafeWriter:
    def __init__(
        self,
        layout: JobLayout,
        run_ts: str,
        manifest: ManifestWriter,
        cipher: crypto.StreamCipher | None = None,
    ) -> None:
        self.layout = layout
        self.run_ts = run_ts
        self.manifest = manifest
        self.cipher = cipher
        self._current = Path(layout.current)
        self._version_root = Path(layout.versions) / run_ts

    # ------------------------------------------------------------------ API pubblica
    def store(self, item: PlannedItem) -> StoredFile:
        if item.action is Action.SKIP:
            raise ValueError("Nulla da scrivere per un elemento SKIP")
        dest = Path(item.dest_path)
        self._check_inside_current(dest)
        self._ensure_parent(dest)
        # 1) la nuova versione viene scritta completamente nel file temporaneo...
        partial, sha, size = self._write_partial(item, dest)
        # 2) ...solo dopo la vecchia viene spostata in versions/ ...
        archived: str | None = None
        if os.path.lexists(long_path(dest)):
            archived = self._archive(dest)
        # 3) ...e la nuova prende il suo posto (rename atomico, mai sovrascrittura).
        rename_no_clobber(partial, dest)
        self.manifest.stored(self._rel(dest), sha, size, item.src.mtime_ns)
        return StoredFile(sha, size, archived)

    def link(self, item: PlannedItem, existing: Path, sha256: str) -> StoredFile:
        """Il file è un rinominato/spostato di ``existing`` (già sul backup, contenuto identico verificato):
        invece di ricopiarlo crea un hard link (nessun dato scritto, nessuno spazio in più).

        Sicuro con il versioning: i file sul backup non vengono mai modificati sul posto (ogni nuova
        versione è un file nuovo + rename), quindi i due nomi non possono "contaminarsi".
        Solleva OSError se il file system non supporta gli hard link (exFAT/FAT32): si ripiega sulla copia.
        """
        if item.action is not Action.COPY:
            raise ValueError("Il collegamento vale solo per file assenti sul backup")
        dest = Path(item.dest_path)
        self._check_inside_current(dest)
        self._check_inside_current(existing)
        self._ensure_parent(dest)
        os.link(long_path(existing), long_path(dest))  # fallisce se dest esiste: mai sovrascrivere
        self.manifest.stored(self._rel(dest), sha256, item.src.size, item.src.mtime_ns)
        self.manifest.renamed(self._rel(existing), self._rel(dest))
        return StoredFile(sha256, item.src.size, None)

    # ------------------------------------------------------------------ interni
    def _rel(self, p: Path) -> str:
        return p.relative_to(self.layout.root).as_posix()

    def _check_inside_current(self, dest: Path) -> None:
        if not is_relative_to(dest, self._current) or ".." in dest.parts:
            raise ValueError(f"Percorso di destinazione fuori da current/: {dest}")

    def _archive(self, existing: Path) -> str:
        """Sposta (rename, stesso volume) un file/cartella di current/ nell'area versioni dell'esecuzione."""
        rel = existing.relative_to(self._current)
        target = self._version_root / rel
        n = 1
        while os.path.lexists(long_path(target)):  # caso limite: stesso percorso archiviato due volte
            n += 1
            target = self._version_root / rel.parent / f"{rel.name}.~{n}"
        os.makedirs(long_path(target.parent), exist_ok=True)
        rename_no_clobber(existing, target)
        self.manifest.archived(self._rel(existing), self._rel(target))
        return str(target)

    def _ensure_parent(self, dest: Path) -> None:
        """Crea le cartelle padre; se un antenato è un file (conflitto tipo) lo archivia prima."""
        parent = dest.parent
        chain: list[Path] = []
        p = parent
        while p != self._current and is_relative_to(p, self._current):
            chain.append(p)
            p = p.parent
        for p in reversed(chain):  # dall'alto verso il basso
            if os.path.isdir(long_path(p)):
                continue
            if os.path.lexists(long_path(p)):
                self._archive(p)  # un file occupa il posto di una cartella
            break
        os.makedirs(long_path(parent), exist_ok=True)

    def _write_partial(self, item: PlannedItem, dest: Path) -> tuple[Path, str, int]:
        src = item.src
        partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
        # "wb" su un .bsys-partial riusa (tronca) un file temporaneo incompleto di una copia interrotta:
        # non contiene mai dati di backup validi.
        with open(long_path(src.abs_path), "rb") as fin, open(long_path(partial), "wb") as fout:
            if self.cipher is not None:
                sha, size = self.cipher.encrypt_stream(fin, fout)
            else:
                h = hashlib.sha256()
                size = 0
                while chunk := fin.read(BUF):
                    h.update(chunk)
                    fout.write(chunk)
                    size += len(chunk)
                sha = h.hexdigest()
            fout.flush()
            os.fsync(fout.fileno())
            st = os.fstat(fin.fileno())
        os.utime(long_path(partial), ns=(st.st_atime_ns, src.mtime_ns))
        if st.st_size != src.size or st.st_mtime_ns != src.mtime_ns or size != src.size:
            # Il file è cambiato durante la copia: il .partial resta e verrà riutilizzato al prossimo giro.
            raise SourceChangedError(src.abs_path)
        return partial, sha, size
