"""Riconoscimento di file rinominati o spostati sul PC.

Un file "nuovo" (assente sul backup) è considerato il rinominato/spostato di un file già presente
in ``current/`` ma non più sul PC (un "orfano") se coincidono:
  1. dimensione (cifrata, se il disco è cifrato),
  2. data di modifica (rinominare/spostare in Esplora file non la cambia; tolleranza 2 s),
  3. impronta SHA-256 del contenuto (letta dal manifest o, se manca, calcolata).

In quel caso non si ricopia: SafeWriter crea un hard link e il manifest registra la rinomina,
così la pagina "Versioni" mostra anche la storia del vecchio nome. Il vecchio nome resta in
``current/`` (i file spariti dal PC non vengono mai rimossi dal backup).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from backupsys.core.fsutil import long_path, mtimes_equal
from backupsys.core.layout import PARTIAL_SUFFIX, JobLayout
from backupsys.core.manifest import Expected, load_expected
from backupsys.core.planner import Action, Plan, sha256_file
from backupsys.security import crypto


@dataclass(frozen=True)
class RenameMatch:
    existing: Path  # file orfano già sul backup con lo stesso contenuto
    sha256: str


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _orphans(layout: JobLayout, planned: set[str]) -> dict[int, list[tuple[Path, int]]]:
    """File in current/ che non corrispondono a nessun file del PC, indicizzati per dimensione."""
    index: dict[int, list[tuple[Path, int]]] = {}
    stack = [str(layout.current)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(long_path(d)) as it:
                for e in it:
                    path = os.path.join(d, e.name)
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(path)
                        elif e.is_file(follow_symlinks=False) and not e.name.endswith(PARTIAL_SUFFIX):
                            if _norm(path) in planned:
                                continue
                            st = e.stat(follow_symlinks=False)
                            if st.st_size > 0:
                                index.setdefault(st.st_size, []).append((Path(path), st.st_mtime_ns))
                    except OSError:
                        continue
        except OSError:
            continue
    return index


def _orphan_sha(
    path: Path, layout: JobLayout, expected: dict[str, Expected], cipher: crypto.StreamCipher | None
) -> str | None:
    rel = path.relative_to(layout.root).as_posix()
    if rel in expected:
        return expected[rel].sha256
    try:  # copiato a mano o manifest mancante: si calcola dal file
        if path.name.endswith(crypto.SUFFIX):
            if cipher is None:
                return None
            with open(long_path(path), "rb") as f:
                return cipher.decrypt_stream(f, None)[0]
        return sha256_file(str(path))
    except (OSError, crypto.DecryptionError):
        return None


def find_renames(
    plan: Plan, layout: JobLayout, encrypted: bool, cipher: crypto.StreamCipher | None
) -> dict[str, RenameMatch]:
    """dest_path dei file da copiare -> file già sul backup da collegare al posto della copia."""
    copies = [i for i in plan.items if i.action is Action.COPY and i.src.size > 0]
    if not copies or not layout.current.is_dir():
        return {}
    index = _orphans(layout, {_norm(i.dest_path) for i in plan.items})
    if not index:
        return {}
    expected: dict[str, Expected] | None = None
    matches: dict[str, RenameMatch] = {}
    for item in copies:
        size = crypto.encrypted_size(item.src.size) if encrypted else item.src.size
        cands = [p for p, mt in index.get(size, []) if mtimes_equal(mt, item.src.mtime_ns)]
        if not cands:
            continue
        # a parità di indizi, preferisci un orfano nella stessa cartella (rinomina) poi altrove (spostamento)
        dest_parent = _norm(str(Path(item.dest_path).parent))
        cands.sort(key=lambda p: _norm(str(p.parent)) != dest_parent)
        try:
            src_sha = sha256_file(item.src.abs_path)
        except OSError:
            continue
        if expected is None:
            expected = load_expected(layout.manifests)
        for cand in cands:
            if _orphan_sha(cand, layout, expected, cipher) == src_sha:
                matches[item.dest_path] = RenameMatch(cand, src_sha)
                break
    return matches
