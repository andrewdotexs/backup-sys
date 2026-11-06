"""Test property-based dell'invariante append-only.

Hypothesis genera sequenze casuali di eventi sul PC e sul disco di backup; dopo ogni backup verifichiamo:
1. ogni file presente sul PC è presente in current/ con lo stesso contenuto;
2. ogni contenuto mai scritto dal programma sul backup è ancora presente (in current/ o in versions/),
   tranne quelli che l'"utente" ha cancellato a mano dal backup.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from backupsys.catalog import db
from tests.conftest import Env

NAMES = st.sampled_from(["a.txt", "b.txt", "d/c.txt", "d/e/f.txt", "d", "x/y.txt"])
CONTENT = st.binary(min_size=0, max_size=64)


def _all_backup_contents(env: Env) -> set[str]:
    root = env.disks[0] / "backup-sys"
    out = set()
    for p in root.rglob("*"):
        if p.is_file() and ".bsys" not in p.parts and not p.name.endswith(".bsys-partial"):
            out.add(hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class AppendOnlyMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["BACKUPSYS_HOME"] = str(self.tmp / "home")
        self.env = Env(self.tmp)
        self.written: set[str] = set()  # hash di contenuti scritti sul backup
        self.user_deleted: set[str] = set()

    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, name: str) -> Path:
        return self.env.src / name

    @rule(name=NAMES, content=CONTENT)
    def write_file(self, name: str, content: bytes) -> None:
        p = self._src(name)
        # se un antenato è un file, lo rimuoviamo dal PC (l'utente riorganizza le cartelle)
        for parent in reversed(p.relative_to(self.env.src).parents):
            q = self.env.src / parent
            if q != self.env.src and q.is_file():
                q.unlink()
        if p.is_dir():
            shutil.rmtree(p)
        self.env.write(name, content)

    @rule(name=NAMES)
    def delete_on_pc(self, name: str) -> None:
        p = self._src(name)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    @rule(name=NAMES)
    def user_deletes_on_backup(self, name: str) -> None:
        p = self.env.current() / name
        if p.is_file():
            self.user_deleted.add(hashlib.sha256(p.read_bytes()).hexdigest())
            p.unlink()

    @rule()
    def backup(self) -> None:
        r = self.env.run()
        assert r.status in (db.OK, db.WARNING), r.message
        for p in self.env.src.rglob("*"):
            if p.is_file():
                b = self.env.current() / p.relative_to(self.env.src)
                assert b.is_file(), f"manca sul backup: {p}"
                assert b.read_bytes() == p.read_bytes()
                self.written.add(hashlib.sha256(p.read_bytes()).hexdigest())

    @invariant()
    def nothing_lost(self) -> None:
        if not (self.env.disks[0] / "backup-sys").exists():
            return
        present = _all_backup_contents(self.env)
        lost = self.written - present - self.user_deleted
        assert not lost, f"contenuti persi dal backup: {lost}"


AppendOnlyMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
TestAppendOnly = AppendOnlyMachine.TestCase
