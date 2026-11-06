"""Un disco di backup che cambia lettera (E: -> F:) viene ritrovato grazie al suo identificativo."""

from __future__ import annotations

from pathlib import Path

import pytest

from backupsys.catalog import db
from backupsys.config.models import Destination, Job, RetentionPolicy
from backupsys.config.store import ConfigStore
from backupsys.destinations import base
from backupsys.service import BackupService, Task
from tests.conftest import Env


@pytest.fixture
def drives(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Finte "lettere di unità": le cartelle di primo livello di tmp_path che il test elenca."""
    vols: list[Path] = []
    monkeypatch.setattr(base, "candidate_volumes", lambda: list(vols))
    return vols


def test_disk_found_on_other_letter(env: Env, tmp_path: Path, drives: list[Path]) -> None:
    env.write("a.txt", "uno")
    env.run()
    old = env.disks[0]
    new = tmp_path / "lettera-G"
    old.rename(new)  # Windows ha assegnato un'altra lettera
    drives += [tmp_path / "lettera-X", new]
    env.write("b.txt", "due")
    r = env.run()
    assert r.status == db.OK, r.message
    assert env.config.destinations[0].path == str(new)  # configurazione aggiornata
    cur = new / "backup-sys" / env.job.slug
    assert any(p.name == "b.txt" for p in cur.rglob("b.txt"))
    assert any(e["action"] == "dest_relocated" for e in env.audit.entries())


def test_wrong_disk_on_old_letter_and_right_one_elsewhere(env: Env, tmp_path: Path, drives: list[Path]) -> None:
    env.write("a.txt", "uno")
    env.run()
    old = env.disks[0]
    new = tmp_path / "lettera-H"
    old.rename(new)
    old.mkdir()  # alla vecchia lettera ora c'è un altro disco (vuoto)
    (old / "foto-personali.jpg").write_text("non toccare")
    drives.append(new)
    r = env.run()
    assert r.status == db.OK and env.config.destinations[0].path == str(new)
    assert sorted(p.name for p in old.iterdir()) == ["foto-personali.jpg"]  # nulla scritto sul disco sbagliato


def test_subfolder_path_is_preserved(tmp_path: Path, drives: list[Path]) -> None:
    disk = tmp_path / "usb"
    (disk / "Backup" / "PC").mkdir(parents=True)
    d = Destination(name="USB", path=str(disk / "Backup" / "PC"))
    vid = base.bind(d)
    moved = tmp_path / "usb-nuova-lettera"
    disk.rename(moved)
    drives.append(moved)
    st = base.check(d.model_copy(update={"volume_id": vid}))
    assert st.ready and st.relocated_to == str(moved / "Backup" / "PC")
    assert "Ritrovato" in st.message


def test_not_found_anywhere(env: Env, tmp_path: Path, drives: list[Path]) -> None:
    env.write("a.txt", "uno")
    env.run()
    env.disks[0].rename(tmp_path / "nascosto")  # non è tra le unità elencate
    drives.append(tmp_path / "altro")
    r = env.run()
    assert r.dests[0].status == db.SKIPPED and r.dests[0].message == "Disco non collegato"


def test_unbound_destination_is_not_searched(tmp_path: Path, drives: list[Path]) -> None:
    other = tmp_path / "altro-disco"
    other.mkdir()
    base.bind(Destination(name="altro", path=str(other)))
    drives.append(other)
    st = base.check(Destination(name="nuova", path=str(tmp_path / "usb-mancante")))
    assert not st.available and st.relocated_to is None


def test_service_persists_relocation(tmp_path: Path, drives: list[Path]) -> None:
    src = tmp_path / "pc"
    src.mkdir()
    (src / "a.txt").write_text("x")
    disk = tmp_path / "usb"
    disk.mkdir()
    svc = BackupService(ConfigStore())
    d = Destination(name="USB", path=str(disk), retention=RetentionPolicy(min_age_days=0))
    svc.save_destination(d)
    j = Job(name="Doc", sources=[str(src)], destinations=[d.id])
    svc.save_job(j)
    svc.execute(Task("backup", j.id))
    moved = tmp_path / "usb-f"
    disk.rename(moved)
    drives.append(moved)
    info = svc.dest_info(svc.config.destination(d.id), max_age=0)
    assert info.status.ready and info.dest.path == str(moved)
    assert ConfigStore().load().destination(d.id).path == str(moved)
    rep = svc.execute(Task("verify", j.id, dest_id=d.id))
    assert rep is not None and getattr(rep, "healthy", False)


def test_real_candidate_volumes_does_not_crash() -> None:
    vols = base.candidate_volumes()
    assert all(v.is_dir() for v in vols)
