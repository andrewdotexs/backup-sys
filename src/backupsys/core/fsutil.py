"""Utilità per il filesystem, con attenzione alle particolarità di Windows."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath, PureWindowsPath

#: Tolleranza sul confronto delle date di modifica (FAT32/exFAT hanno risoluzione 2 s, SMB può arrotondare).
MTIME_TOLERANCE_NS = 2_000_000_000

IS_WINDOWS = os.name == "nt"


def long_path(path: str | os.PathLike[str]) -> str:
    """Restituisce il percorso con prefisso ``\\\\?\\`` su Windows, per superare il limite dei 260 caratteri."""
    s = os.fspath(path)
    if not IS_WINDOWS:
        return s
    if s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):  # UNC: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def display_path(path: str | os.PathLike[str]) -> str:
    """Percorso leggibile per l'utente: senza il prefisso tecnico ``\\\\?\\``."""
    s = os.fspath(path)
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def is_link_or_junction(path: str) -> bool:
    try:
        return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda p: False)(path))
    except OSError:
        return False


def real_folder(path: str) -> str:
    """Cartella reale: risolve link e junction (es. il collegamento di compatibilità
    ``C:\\Users\\andrea\\Documenti`` → ``C:\\Users\\andrea\\Documents``, che Windows non permette di elencare)."""
    return os.path.realpath(path) if is_link_or_junction(path) else path


def source_key(source: str) -> str:
    """Trasforma un percorso sorgente in una chiave relativa stabile usata sul disco di backup.

    ``C:\\Users\\andrea\\Documenti`` -> ``C/Users/andrea/Documenti``
    ``\\\\NAS\\share\\foto``          -> ``UNC/NAS/share/foto``
    ``/home/andrea/doc``              -> ``ROOT/home/andrea/doc``
    """
    if re.match(r"^[A-Za-z]:[\\/]", source) or source.startswith("\\\\"):
        wp = PureWindowsPath(source)
        if wp.drive.startswith("\\\\"):
            server_share = wp.drive.strip("\\").split("\\")
            parts = ["UNC", *server_share, *wp.parts[1:]]
        else:
            parts = [wp.drive.rstrip(":").upper(), *wp.parts[1:]]
    else:
        pp = PurePath(os.path.abspath(source))
        parts = ["ROOT", *pp.parts[1:]]
    cleaned = [p for p in parts if p not in ("", "\\", "/")]
    return "/".join(cleaned)


def mtimes_equal(a_ns: int, b_ns: int) -> bool:
    return abs(a_ns - b_ns) <= MTIME_TOLERANCE_NS


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def rename_no_clobber(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Rinomina ``src`` in ``dst`` **senza mai sovrascrivere** un file esistente.

    Su Windows ``os.rename`` fallisce già se la destinazione esiste. Su POSIX va controllato:
    l'esecuzione è serializzata dal lock di destinazione, quindi il controllo è sufficiente.
    """
    s, d = long_path(src), long_path(dst)
    if not IS_WINDOWS and os.path.lexists(d):
        raise FileExistsError(f"La destinazione esiste già: {dst}")
    os.rename(s, d)


def disk_usage(path: str | os.PathLike[str]) -> tuple[int, int, int]:
    """(totale, usato, libero) in byte."""
    import shutil

    u = shutil.disk_usage(long_path(path))
    return u.total, u.used, u.free


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
