from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from backupsys.catalog.db import Catalog
from backupsys.config.models import AppConfig, Destination, Job, RetentionPolicy
from backupsys.core.engine import BackupEngine
from backupsys.security import crypto, secrets
from backupsys.security.audit import AuditLog


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("BACKUPSYS_HOME", str(home))

    # Ogni cartella di primo livello in tmp_path fa da "volume" (un disco a sé), così il controllo
    # "disco vuoto" non guarda l'intero file system del computer che esegue i test.
    from backupsys.destinations import base as dest_base

    def fake_volume_root(path: str) -> Path:
        p = Path(os.path.abspath(path))
        for anc in [p, *p.parents]:
            if anc.parent == tmp_path:
                return anc
        return p

    monkeypatch.setattr(dest_base, "volume_root", fake_volume_root)
    secrets.use_memory_backend()
    # parametri Argon2 leggeri nei test
    monkeypatch.setattr(crypto, "ARGON2_PARAMS", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    yield home


class Env:
    """Ambiente di prova: una sorgente (il "PC") e N dischi di backup."""

    def __init__(self, tmp: Path, n_dest: int = 1) -> None:
        self.src = tmp / "pc" / "Documenti"
        self.src.mkdir(parents=True)
        self.disks = []
        for i in range(n_dest):
            d = tmp / f"disk{i}"
            d.mkdir()
            self.disks.append(d)
        self.config = AppConfig(
            destinations=[
                Destination(name=f"Disco {i}", path=str(d), retention=RetentionPolicy(min_age_days=0))
                for i, d in enumerate(self.disks)
            ],
        )
        self.job = Job(name="Documenti", sources=[str(self.src)], excludes=["*.tmp"], guard_min_files=1000)
        self.job.destinations = [d.id for d in self.config.destinations]
        self.config.jobs.append(self.job)
        self.catalog = Catalog()
        self.audit = AuditLog()
        self.engine = BackupEngine(self.catalog, self.audit, on_bind=self._bind)
        self._mtime = int(time.time()) - 100_000

    def _bind(self, dest_id: str, vid: str) -> None:
        d = self.config.destination(dest_id)
        d.volume_id = vid

    def write(self, rel: str, content: bytes | str) -> Path:
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        p.write_bytes(data)
        self._mtime += 10  # mtime distinti oltre la tolleranza di 2 s
        os.utime(p, (self._mtime, self._mtime))
        return p

    def run(self, **kw: object):  # type: ignore[no-untyped-def]
        from backupsys.core.engine import RunOptions

        return self.engine.run(self.job, self.config.destinations, RunOptions(**kw))  # type: ignore[arg-type]

    def current(self, disk: int = 0) -> Path:
        from backupsys.core.fsutil import source_key

        return self.disks[disk] / "backup-sys" / self.job.slug / "current" / source_key(str(self.src))

    def versions(self, disk: int = 0) -> Path:
        return self.disks[disk] / "backup-sys" / self.job.slug / "versions"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def env2(tmp_path: Path) -> Env:
    return Env(tmp_path, n_dest=2)
