"""Test statico: nessuna API di cancellazione/sovrascrittura nel codice, tranne dove esplicitamente ammesso.

È la garanzia "per costruzione" dell'invariante append-only: se qualcuno in futuro aggiunge
un ``os.remove`` nel motore di backup, questo test fallisce.
"""

from __future__ import annotations

import ast
from pathlib import Path

import backupsys

PKG = Path(backupsys.__file__).parent

DELETE_CALLS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "replace"),  # sovrascrive la destinazione
    ("shutil", "rmtree"),
    ("shutil", "move"),  # può sovrascrivere
    ("shutil", "copyfile"),  # sovrascrive
    ("shutil", "copy"),
    ("shutil", "copy2"),
}
DELETE_METHODS = {"unlink", "rmdir", "replace", "rename"}  # metodi di pathlib.Path (rename sovrascrive su POSIX)

#: Uniche eccezioni ammesse, con motivazione.
ALLOWED = {
    ("core/retention.py", "shutil.rmtree"): "rotazione versioni (D3), con controlli sul percorso",
    ("config/store.py", "os.replace"): "salvataggio atomico del file di configurazione locale",
    ("security/auth.py", "os.replace"): "salvataggio atomico dell'hash password locale",
}


def _violations() -> list[str]:
    found: list[str] = []
    for py in sorted(PKG.rglob("*.py")):
        rel = py.relative_to(PKG).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                base = node.func.value
                if isinstance(base, ast.Name) and (base.id, attr) in DELETE_CALLS:
                    name = f"{base.id}.{attr}"
                    if (rel, name) not in ALLOWED:
                        found.append(f"{rel}:{node.lineno} {name}")
                elif (
                    attr in DELETE_METHODS
                    and not (isinstance(base, ast.Name) and base.id == "os")
                    # str.replace(a, b) ha 2+ argomenti; Path.replace/rename(target) ne ha 1, unlink/rmdir 0
                    and len(node.args) <= 1
                ):
                    # metodo su un oggetto (es. Path(...).unlink()) — os.rename è ammesso solo in fsutil
                    found.append(f"{rel}:{node.lineno} .{attr}()")
                elif isinstance(base, ast.Name) and base.id == "os" and attr == "rename" and rel != "core/fsutil.py":
                    found.append(f"{rel}:{node.lineno} os.rename (usa fsutil.rename_no_clobber)")
            if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil"):
                for alias in node.names:
                    if (node.module, alias.name) in DELETE_CALLS:
                        found.append(f"{rel}:{node.lineno} from {node.module} import {alias.name}")
    return found


def test_no_delete_apis_outside_allowlist() -> None:
    assert _violations() == []


def test_allowlist_entries_are_real() -> None:
    """Le eccezioni devono corrispondere a codice esistente (niente voci obsolete)."""
    for rel, name in ALLOWED:
        assert name.split(".")[1] in (PKG / rel).read_text(encoding="utf-8"), (rel, name)
