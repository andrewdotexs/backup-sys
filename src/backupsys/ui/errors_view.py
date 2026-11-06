"""Raggruppamento per cartella dei file saltati, per l'esclusione rapida dallo storico."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any

from backupsys.config.models import Job
from backupsys.core.fsutil import IS_WINDOWS, real_folder
from backupsys.core.scanner import is_absolute_exclude, norm_abs


def _parent(path: str) -> str:
    if IS_WINDOWS or "\\" in path:
        return str(PureWindowsPath(path).parent)
    return os.path.dirname(path)


@dataclass
class ErrorGroup:
    folder: str
    items: list[dict[str, Any]] = field(default_factory=list)
    #: livelli escludibili: la cartella stessa e le superiori, fino alla cartella sorgente (esclusa)
    choices: list[str] = field(default_factory=list)
    excluded: bool = False
    root: str = ""  # cartella sorgente che contiene il gruppo (per mostrare percorsi brevi)

    def short(self, path: str) -> str:
        """``path`` relativo alla sorgente (``AppData\\Local\\...``), o il percorso intero se esterno."""
        if self.root and norm_abs(path).startswith(norm_abs(self.root)):
            rest = path[len(self.root) :].lstrip("\\/")
            return rest or "(cartella principale)"
        return path

    @property
    def labelled_choices(self) -> list[tuple[str, str]]:
        return [(c, self.short(c)) for c in self.choices]

    @property
    def excludable(self) -> bool:
        return bool(self.choices) and not self.excluded


def _is_excluded(path: str, excluded: set[str]) -> bool:
    p = norm_abs(path)
    while True:
        if p in excluded:
            return True
        parent = os.path.dirname(p)
        if parent == p or not parent:
            return False
        p = parent


def group_errors(errors: list[dict[str, Any]], job: Job | None) -> tuple[list[ErrorGroup], list[dict[str, Any]]]:
    """(gruppi per cartella, errori non raggruppabili). Ogni errore riceve ``excludable`` ed ``excluded``."""
    roots: list[str] = []
    excluded: set[str] = set()
    if job is not None:
        for s in job.sources:
            roots += [norm_abs(s), norm_abs(real_folder(s))]
        excluded = {norm_abs(e) for e in job.excludes if is_absolute_exclude(e)}

    display_roots: dict[str, str] = {}
    if job is not None:
        for s in job.sources:
            display_roots[norm_abs(s)] = s
            display_roots[norm_abs(real_folder(s))] = real_folder(s)

    def inside_root(norm: str) -> str | None:
        for r in roots:
            if norm != r and (norm.startswith(r.rstrip("\\/") + os.sep) or norm.startswith(r.rstrip("\\/") + "/")):
                return r
        return None

    groups: dict[str, ErrorGroup] = {}
    loose: list[dict[str, Any]] = []
    for e in errors:
        path = str(e.get("path", ""))
        e["excludable"] = job is not None and is_absolute_exclude(path) and inside_root(norm_abs(path)) is not None
        e["excluded"] = e["excludable"] and _is_excluded(path, excluded)
        if not is_absolute_exclude(path):
            loose.append(e)
            continue
        folder = _parent(path)
        key = norm_abs(folder)
        g = groups.get(key)
        if g is None:
            g = groups[key] = ErrorGroup(folder)
            root = inside_root(key) or (key if key in display_roots else None)
            g.root = display_roots.get(root, "") if root else ""
            root = inside_root(key)
            if root is not None:
                cur = folder
                while inside_root(norm_abs(cur)) is not None:
                    g.choices.append(cur)
                    nxt = _parent(cur)
                    if nxt == cur:
                        break
                    cur = nxt
            g.excluded = _is_excluded(folder, excluded)
        g.items.append(e)
    ordered = sorted(groups.values(), key=lambda g: (-len(g.items), norm_abs(g.folder)))
    return ordered, loose
