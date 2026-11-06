"""Scansione delle cartelle sorgente con esclusioni stile glob."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache

from backupsys.core.fsutil import IS_WINDOWS, display_path, long_path, real_folder, source_key
from backupsys.core.layout import MARKER_NAME, ROOT_DIR


@dataclass(frozen=True, slots=True)
class SourceFile:
    key: str  # chiave della sorgente (es. "C/Users/andrea/Documenti")
    rel: str  # percorso relativo alla sorgente, separatore "/"
    abs_path: str
    size: int
    mtime_ns: int

    @property
    def backup_rel(self) -> str:
        """Percorso relativo dentro ``current/``."""
        return f"{self.key}/{self.rel}"


@dataclass
class ScanResult:
    files: list[SourceFile] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (percorso, messaggio)
    excluded: int = 0
    unreadable_sources: list[str] = field(default_factory=list)  # cartelle sorgente non leggibili del tutto

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    p = pattern.replace("\\", "/").strip()
    if "/" not in p:
        p = "**/" + p  # senza "/" il pattern vale per il nome in qualsiasi cartella
    out = []
    i = 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    flags = re.IGNORECASE if IS_WINDOWS else 0
    return re.compile("^" + "".join(out) + "$", flags)


class Excluder:
    def __init__(self, patterns: list[str]) -> None:
        self.patterns = [_compile(p) for p in patterns if p.strip()]

    def file_excluded(self, rel: str) -> bool:
        return any(p.match(rel) for p in self.patterns)

    def dir_excluded(self, rel: str) -> bool:
        probe = rel + "/"
        return any(p.match(rel) or p.match(probe) for p in self.patterns)


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


def scan_source(source: str, excluder: Excluder, follow_symlinks: bool, result: ScanResult) -> None:
    key = source_key(source)  # la chiave resta quella del percorso configurato
    real = real_folder(source)
    root = long_path(real)
    if not os.path.isdir(root):
        result.errors.append((source, "Cartella sorgente non trovata o non accessibile"))
        result.unreadable_sources.append(source)
        return
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        dir_abs, dir_rel = stack.pop()
        try:
            it = os.scandir(dir_abs)
        except OSError as e:
            msg = f"Impossibile leggere la cartella: {e.strerror or e}"
            if not dir_rel:
                result.unreadable_sources.append(source)
            result.errors.append((display_path(dir_abs), msg))
            continue
        with it:
            for entry in it:
                rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
                try:
                    if not follow_symlinks and _is_link(entry):
                        continue
                    if entry.is_dir(follow_symlinks=follow_symlinks):
                        if entry.name == ROOT_DIR and os.path.exists(os.path.join(entry.path, MARKER_NAME)):
                            continue  # non fare il backup di un disco di backup dentro la sorgente
                        if excluder.dir_excluded(rel):
                            result.excluded += 1
                            continue
                        stack.append((entry.path, rel))
                    elif entry.is_file(follow_symlinks=follow_symlinks):
                        if entry.name.endswith(".bsys-partial") or excluder.file_excluded(rel):
                            result.excluded += 1
                            continue
                        st = entry.stat(follow_symlinks=follow_symlinks)
                        result.files.append(SourceFile(key, rel, entry.path, st.st_size, st.st_mtime_ns))
                except OSError as e:
                    result.errors.append((display_path(entry.path), f"Errore di accesso: {e.strerror or e}"))


def scan(sources: list[str], excludes: list[str], follow_symlinks: bool = False) -> ScanResult:
    result = ScanResult()
    excluder = Excluder(excludes)
    for s in sources:
        scan_source(s, excluder, follow_symlinks, result)
    result.files.sort(key=lambda f: f.backup_rel)
    return result


def iter_files(result: ScanResult) -> Iterator[SourceFile]:
    yield from result.files
