"""Navigazione dei backup e ripristino di file/cartelle/versioni.

Il ripristino scrive sul PC e anche qui non sovrascrive mai: se il file esiste già, il ripristinato
prende il nome ``nome (ripristinato AAAA-MM-GG HH-MM).ext``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from backupsys.config.models import Destination, Job
from backupsys.core.fsutil import is_relative_to, long_path, source_key
from backupsys.core.layout import CURRENT, VERSIONS, DestLayout, JobLayout
from backupsys.security import crypto

BUF = 1024 * 1024


@dataclass(frozen=True)
class Entry:
    name: str  # nome visualizzato (senza .bsenc)
    rel: str  # percorso relativo all'area
    is_dir: bool
    size: int
    mtime: datetime


@dataclass(frozen=True)
class FileVersion:
    area: str  # "current" oppure "versions/<ts>"
    label: str
    rel: str
    size: int
    mtime: datetime


@dataclass
class RestoreReport:
    restored: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def job_layout(dest: Destination, job: Job) -> JobLayout:
    return DestLayout(Path(dest.path)).job(job.slug)


def area_root(layout: JobLayout, area: str) -> Path:
    if area == CURRENT:
        return layout.current
    parts = area.split("/")
    if len(parts) == 2 and parts[0] == VERSIONS and parts[1] in layout.version_runs():
        return layout.versions / parts[1]
    raise ValueError(f"Area non valida: {area}")


def _safe_join(root: Path, rel: str) -> Path:
    rel = rel.strip("/")
    if any(p in ("..", "") for p in PurePosixPath(rel).parts) and rel:
        raise ValueError("Percorso non valido")
    p = root.joinpath(*PurePosixPath(rel).parts) if rel else root
    if not is_relative_to(p, root):
        raise ValueError("Percorso non valido")
    return p


def areas(layout: JobLayout) -> list[str]:
    return [CURRENT] + [f"{VERSIONS}/{ts}" for ts in reversed(layout.version_runs())]


def browse(layout: JobLayout, area: str, rel: str = "") -> list[Entry]:
    root = area_root(layout, area)
    d = _safe_join(root, rel)
    out: list[Entry] = []
    if not d.is_dir():
        return out
    with os.scandir(long_path(d)) as it:
        for e in it:
            if e.name.endswith(".bsys-partial") or e.is_symlink():
                continue
            st = e.stat(follow_symlinks=False)
            is_dir = e.is_dir(follow_symlinks=False)
            name = e.name[: -len(crypto.SUFFIX)] if not is_dir and e.name.endswith(crypto.SUFFIX) else e.name
            out.append(
                Entry(
                    name,
                    f"{rel.strip('/')}/{e.name}".strip("/"),
                    is_dir,
                    st.st_size,
                    datetime.fromtimestamp(st.st_mtime),
                )
            )
    out.sort(key=lambda x: (not x.is_dir, x.name.lower()))
    return out


def file_versions(layout: JobLayout, rel: str) -> list[FileVersion]:
    """Tutte le copie disponibili di un file (``rel`` relativo a current/ o a una versione)."""
    out: list[FileVersion] = []
    for area in areas(layout):
        p = _safe_join(area_root(layout, area), rel)
        if p.is_file():
            st = p.stat()
            if area == CURRENT:
                label = "Copia corrente"
            else:
                d, t = area.split("/")[1].split("T")
                label = f"Versione del {d} {t.replace('-', ':')[:8]}"
            out.append(FileVersion(area, label, rel, st.st_size, datetime.fromtimestamp(st.st_mtime)))
    return out


def source_roots(layout: JobLayout, job: Job, area: str) -> list[Entry]:
    """Radice della navigazione: le cartelle sorgente del job (con il loro percorso originale come nome)."""
    root = area_root(layout, area)
    out = []
    for src in job.sources:
        key = source_key(src)
        p = _safe_join(root, key)
        if p.is_dir():
            out.append(Entry(src, key, True, 0, datetime.fromtimestamp(p.stat().st_mtime)))
    return out


def crumbs(job: Job, rel: str) -> list[tuple[str, str]]:
    """Briciole di pane: la cartella sorgente originale come primo elemento, poi le sottocartelle."""
    rel = rel.strip("/")
    for src in job.sources:
        key = source_key(src)
        if rel == key or rel.startswith(key + "/"):
            out = [(src, key)]
            acc = key
            for part in [x for x in rel[len(key) :].split("/") if x]:
                acc = f"{acc}/{part}"
                out.append((part, acc))
            return out
    out, acc = [], ""
    for part in [x for x in rel.split("/") if x]:
        acc = f"{acc}/{part}".strip("/")
        out.append((part, acc))
    return out


def original_location(job: Job, rel: str) -> Path | None:
    """Ricava il percorso originale sul PC da un percorso relativo all'area (``<chiave>/<resto>``)."""
    rel = rel.strip("/")
    for src in job.sources:
        key = source_key(src)
        if rel == key or rel.startswith(key + "/"):
            rest = rel[len(key) :].strip("/")
            if rest.endswith(crypto.SUFFIX):
                rest = rest[: -len(crypto.SUFFIX)]
            return Path(src).joinpath(*rest.split("/")) if rest else Path(src)
    return None


def _unique_target(target: Path) -> Path:
    if not os.path.lexists(long_path(target)):
        return target
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M")
    stem, suffix = target.stem, target.suffix
    candidate = target.with_name(f"{stem} (ripristinato {stamp}){suffix}")
    n = 2
    while os.path.lexists(long_path(candidate)):
        candidate = target.with_name(f"{stem} (ripristinato {stamp} {n}){suffix}")
        n += 1
    return candidate


def _restore_file(src: Path, target: Path, cipher: crypto.StreamCipher | None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _unique_target(target)
    encrypted = src.name.endswith(crypto.SUFFIX)
    if encrypted and cipher is None:
        raise PermissionError("File cifrato: chiave non disponibile")
    with open(long_path(src), "rb") as fin, open(long_path(target), "xb") as fout:  # "x": mai sovrascrivere
        if encrypted:
            assert cipher is not None
            cipher.decrypt_stream(fin, fout)
        else:
            while chunk := fin.read(BUF):
                fout.write(chunk)
    st = src.stat()
    os.utime(long_path(target), ns=(st.st_atime_ns, st.st_mtime_ns))
    return target


def restore(
    layout: JobLayout,
    job: Job,
    area: str,
    rel: str,
    target_dir: Path | None,
    cipher: crypto.StreamCipher | None,
) -> RestoreReport:
    """Ripristina ``rel`` (file o cartella) da ``area``.

    ``target_dir`` None = posizione originale; altrimenti dentro ``target_dir``.
    """
    report = RestoreReport()
    src = _safe_join(area_root(layout, area), rel)
    if not os.path.lexists(src):
        report.errors.append((rel, "Non trovato sul backup"))
        return report
    if target_dir is None:
        dest = original_location(job, rel)
        if dest is None:
            report.errors.append((rel, "Posizione originale sconosciuta: scegli una cartella di destinazione"))
            return report
    else:
        name = src.name[: -len(crypto.SUFFIX)] if src.name.endswith(crypto.SUFFIX) else src.name
        dest = target_dir / name

    if src.is_file():
        items = [(src, dest)]
    else:
        items = []
        for dirpath, _dirs, files in os.walk(src):
            for fn in files:
                if fn.endswith(".bsys-partial"):
                    continue
                f = Path(dirpath) / fn
                relp = f.relative_to(src)
                out_name = fn[: -len(crypto.SUFFIX)] if fn.endswith(crypto.SUFFIX) else fn
                items.append((f, dest / relp.parent / out_name))
    for s, t in items:
        try:
            report.restored.append(str(_restore_file(s, t, cipher)))
        except (OSError, crypto.DecryptionError) as e:
            report.errors.append((str(s), str(e)))
    return report
