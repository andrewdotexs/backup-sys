"""Verifica di integrità: rilegge i file sul backup e ne confronta l'hash con il manifest."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from backupsys.core.fsutil import long_path
from backupsys.core.layout import CURRENT, JobLayout
from backupsys.core.manifest import load_expected
from backupsys.core.planner import sha256_file
from backupsys.security import crypto


@dataclass
class VerifyReport:
    checked: int = 0
    ok: int = 0
    corrupted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unverifiable: int = 0  # file presenti ma senza hash nel manifest (es. copiati a mano)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.corrupted and not self.errors


def verify(
    layout: JobLayout,
    cipher: crypto.StreamCipher | None,
    include_versions: bool = False,
    cancel: threading.Event | None = None,
) -> VerifyReport:
    report = VerifyReport()
    expected = load_expected(layout.manifests)
    for rel, exp in sorted(expected.items()):
        if cancel and cancel.is_set():
            break
        if not include_versions and not rel.startswith(CURRENT + "/"):
            continue
        path = layout.root.joinpath(*rel.split("/"))
        if not os.path.isfile(long_path(path)):
            if rel.startswith(CURRENT + "/"):
                report.missing.append(rel)  # cancellato a mano: verrà ricopiato al prossimo backup
            continue
        report.checked += 1
        try:
            if rel.endswith(crypto.SUFFIX):
                if cipher is None:
                    report.errors.append((rel, "Chiave non disponibile"))
                    continue
                with open(long_path(path), "rb") as f:
                    sha, _ = cipher.decrypt_stream(f, None)
            else:
                sha = sha256_file(str(path))
        except crypto.DecryptionError:
            report.corrupted.append(rel)
            continue
        except OSError as e:
            report.errors.append((rel, e.strerror or str(e)))
            continue
        if sha == exp.sha256:
            report.ok += 1
        else:
            report.corrupted.append(rel)
    known = set(expected)
    for dirpath, _dirs, files in os.walk(layout.current):
        for fn in files:
            if fn.endswith(".bsys-partial"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), layout.root).replace(os.sep, "/")
            if rel not in known:
                report.unverifiable += 1
    return report
