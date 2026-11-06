"""Rotazione delle vecchie versioni (D3) — L'UNICO modulo del programma che può cancellare.

Regole (verificate dai test):
- cancella SOLO cartelle intere ``<disco>/backup-sys/<job>/versions/<timestamp>``;
- MAI nulla dentro ``current/``;
- MAI versioni più giovani di ``min_age_days``, né quelle dell'esecuzione in corso;
- si ferma appena rientra nei limiti; se non può rientrare, non cancella altro.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from backupsys.config.models import RetentionPolicy
from backupsys.core.fsutil import disk_usage, long_path
from backupsys.core.layout import CURRENT, ROOT_DIR, TS_RE, VERSIONS, DestLayout, parse_timestamp


class RetentionSafetyError(Exception):
    pass


@dataclass
class RetentionReport:
    deleted: list[tuple[str, int]] = field(default_factory=list)  # (cartella, byte liberati)
    versions_bytes_before: int = 0
    versions_bytes_after: int = 0
    within_limits: bool = True
    message: str = ""

    @property
    def freed_bytes(self) -> int:
        return sum(b for _, b in self.deleted)


def tree_size(path: Path) -> int:
    total = 0
    stack = [long_path(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _assert_deletable(folder: Path, backup_root: Path) -> None:
    """Controllo di sicurezza sul percorso RISOLTO prima di ogni cancellazione."""
    real = folder.resolve()
    root = backup_root.resolve()
    if folder.is_symlink():
        raise RetentionSafetyError(f"Rifiuto: {folder} è un link")
    if not TS_RE.match(real.name):
        raise RetentionSafetyError(f"Rifiuto: {real.name} non è una cartella di versione")
    if real.parent.name != VERSIONS:
        raise RetentionSafetyError(f"Rifiuto: {real} non è dentro versions/")
    job_root = real.parent.parent
    if job_root.parent != root or root.name != ROOT_DIR:
        raise RetentionSafetyError(f"Rifiuto: {real} non è nella struttura backup-sys")
    if CURRENT in real.relative_to(root).parts:
        raise RetentionSafetyError(f"Rifiuto: {real} è dentro current/")


@dataclass(frozen=True)
class _Candidate:
    folder: Path
    ts: str
    when: datetime


def _candidates(layout: DestLayout) -> list[_Candidate]:
    out: list[_Candidate] = []
    for job_root in layout.job_roots():
        vdir = job_root / VERSIONS
        if not vdir.is_dir():
            continue
        for p in vdir.iterdir():
            if p.is_dir() and not p.is_symlink() and TS_RE.match(p.name):
                out.append(_Candidate(p, p.name, parse_timestamp(p.name)))
    out.sort(key=lambda c: (c.when, c.ts))
    return out


def enforce(
    layout: DestLayout,
    policy: RetentionPolicy,
    reserve_bytes: int = 0,
    protect: set[str] | None = None,
    now: datetime | None = None,
    usage: Callable[[Path], tuple[int, int, int]] | None = None,
    log: Callable[[str, int], None] | None = None,
) -> RetentionReport:
    """Ruota le versioni più vecchie finché il disco rientra nei limiti della ``policy``.

    ``reserve_bytes``: spazio che l'esecuzione imminente dovrà scrivere (si considera già occupato).
    ``usage``: funzione (totale, usato, libero) iniettabile nei test.
    """
    usage = usage or (lambda p: disk_usage(p))
    now = now or datetime.now()
    protect = protect or set()
    min_age = timedelta(days=policy.min_age_days)
    report = RetentionReport()

    cands = _candidates(layout)
    sizes = {c.folder: tree_size(c.folder) for c in cands}
    versions_bytes = sum(sizes.values())
    report.versions_bytes_before = versions_bytes

    def over_limit() -> bool:
        total, _used, free = usage(layout.dest_path)  # riletto ogni volta: riflette ciò che è stato liberato
        free_after = free - reserve_bytes
        too_many_versions = versions_bytes > total * policy.max_versions_percent / 100
        too_little_free = free_after < total * policy.min_free_percent / 100
        return too_many_versions or too_little_free

    for c in cands:
        if not over_limit():
            break
        if c.ts in protect or now - c.when < min_age:
            continue
        _assert_deletable(c.folder, layout.root)
        size = sizes[c.folder]
        shutil.rmtree(long_path(c.folder))
        versions_bytes -= size
        report.deleted.append((str(c.folder), size))
        if log:
            log(str(c.folder), size)
    report.versions_bytes_after = versions_bytes
    report.within_limits = not over_limit()
    if not report.within_limits:
        report.message = (
            "Limiti di spazio non rispettati: le versioni rimaste sono troppo recenti per essere ruotate "
            f"(età minima {policy.min_age_days} giorni) oppure la copia corrente occupa troppo spazio."
        )
    return report
