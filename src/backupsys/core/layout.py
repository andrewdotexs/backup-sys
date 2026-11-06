"""Struttura delle cartelle su un disco di backup.

<DESTINAZIONE>/backup-sys/
├── .bsys-volume.json            marker con l'ID del disco
├── .bsys-key.json               (solo se cifrata) chiave dati protetta da passphrase
└── <job-slug>/
    ├── current/<chiave-sorgente>/...      copia speculare
    ├── versions/<timestamp>/<chiave>/...  versioni precedenti (ruotate da retention.py)
    └── .bsys/manifests/<timestamp>.jsonl  hash dei file scritti in ogni esecuzione
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = "backup-sys"
MARKER_NAME = ".bsys-volume.json"
CURRENT = "current"
VERSIONS = "versions"
META = ".bsys"
PARTIAL_SUFFIX = ".bsys-partial"

TS_FORMAT = "%Y-%m-%dT%H-%M-%S"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$")


def run_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(TS_FORMAT)


def parse_timestamp(ts: str) -> datetime:
    return datetime.strptime(ts.split("_")[0], TS_FORMAT)


@dataclass(frozen=True)
class DestLayout:
    dest_path: Path

    @property
    def root(self) -> Path:
        return self.dest_path / ROOT_DIR

    @property
    def marker(self) -> Path:
        return self.root / MARKER_NAME

    def job(self, slug: str) -> JobLayout:
        return JobLayout(self.root / slug)

    def job_roots(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir() and (p / CURRENT).is_dir())


@dataclass(frozen=True)
class JobLayout:
    root: Path

    @property
    def current(self) -> Path:
        return self.root / CURRENT

    @property
    def versions(self) -> Path:
        return self.root / VERSIONS

    @property
    def meta(self) -> Path:
        return self.root / META

    @property
    def manifests(self) -> Path:
        return self.meta / "manifests"

    def version_runs(self) -> list[str]:
        if not self.versions.is_dir():
            return []
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir() and TS_RE.match(p.name))

    def unique_run_ts(self, ts: str) -> str:
        """Evita collisioni se due esecuzioni partono nello stesso secondo."""
        candidate, n = ts, 1
        while (self.versions / candidate).exists() or (self.manifests / f"{candidate}.jsonl").exists():
            n += 1
            candidate = f"{ts}_{n}"
        return candidate
