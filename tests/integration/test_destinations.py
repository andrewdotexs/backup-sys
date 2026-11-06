"""Inizializzazione dei dischi: solo dischi vuoti, sottocartelle create, dischi già inizializzati riconosciuti."""

from __future__ import annotations

from pathlib import Path

import pytest

from backupsys.catalog import db
from backupsys.config.models import Destination
from backupsys.destinations import base
from tests.conftest import Env


def _dest(path: Path, **kw: object) -> Destination:
    return Destination(name="USB", path=str(path), **kw)  # type: ignore[arg-type]


def test_empty_disk_is_accepted(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    disk.mkdir()
    (disk / "System Volume Information").mkdir()  # creata da Windows: non conta
    (disk / "$RECYCLE.BIN").mkdir()
    st = base.check(_dest(disk))
    assert st.ready and "vuoto" in st.message


def test_disk_with_other_files_is_refused(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    (disk / "Foto_mie").mkdir(parents=True)
    (disk / "Foto_mie" / "x.jpg").write_text("mia foto")
    (disk / "documento.docx").write_text("doc")
    d = _dest(disk)
    st = base.check(d)
    assert st.available and not st.identity_ok
    assert "non è vuoto" in st.message and "Foto_mie" in st.message and "formattato" in st.message
    with pytest.raises(base.DiskNotEmptyError):
        base.bind(d)
    assert not (disk / "backup-sys").exists()
    assert (disk / "Foto_mie" / "x.jpg").read_text() == "mia foto"


def test_missing_subfolder_on_empty_disk_is_created(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    disk.mkdir()
    d = _dest(disk / "Backup" / "PC-Andrea")
    st = base.check(d)
    assert st.ready, st.message
    vid = base.bind(d)
    assert (disk / "Backup" / "PC-Andrea" / "backup-sys" / ".bsys-volume.json").exists() and vid


def test_subfolder_on_disk_with_other_files_is_refused(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    disk.mkdir()
    (disk / "Film").mkdir()
    st = base.check(_dest(disk / "Backup"))
    assert not st.identity_ok and "Film" in st.message


def test_existing_non_empty_subfolder_is_refused(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    (disk / "Backup").mkdir(parents=True)
    (disk / "Backup" / "vecchio.zip").write_text("z")
    st = base.check(_dest(disk / "Backup"))
    assert not st.identity_ok and "vecchio.zip" in st.message


def test_unplugged_disk_with_subfolder_is_not_available(tmp_path: Path) -> None:
    st = base.check(_dest(tmp_path / "usb-staccato" / "Backup"))
    assert not st.available and st.message == "Disco non collegato"


def test_already_initialised_disk_is_adopted_even_if_user_added_files(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    disk.mkdir()
    vid = base.bind(_dest(disk))
    (disk / "aggiunto-dopo.txt").write_text("x")
    st = base.check(_dest(disk))  # nuova destinazione in configurazione, stesso disco
    assert st.ready and st.volume_id == vid
    assert base.check(_dest(disk, volume_id=vid)).ready


def test_bound_destination_with_missing_folder_is_not_recreated(tmp_path: Path) -> None:
    disk = tmp_path / "usb"
    disk.mkdir()
    d = _dest(disk / "Backup")
    vid = base.bind(d)
    import shutil

    shutil.rmtree(disk / "Backup")
    st = base.check(_dest(disk / "Backup", volume_id=vid))
    assert not st.ready
    assert not (disk / "Backup").exists()


def test_engine_refuses_non_empty_disk(env: Env) -> None:
    (env.disks[0] / "foto-personali").mkdir()
    env.write("a.txt", "x")
    r = env.run()
    assert r.dests[0].status == db.FAILED and "non è vuoto" in r.dests[0].message
    assert not (env.disks[0] / "backup-sys").exists()
    assert env.config.destinations[0].volume_id is None


def test_engine_quota_exceeded(env: Env) -> None:
    env.config.destinations[0].retention.max_backup_gb = 1000 / 2**30  # ~1000 byte
    env.write("grande.bin", b"x" * 5000)
    r = env.run()
    assert r.dests[0].status == db.FAILED and "Quota" in r.dests[0].message
    assert not (env.current() / "grande.bin").exists()


def test_engine_records_usage(env: Env) -> None:
    env.write("a.bin", b"x" * 3000)
    env.run()
    usage = env.catalog.usage(env.config.destinations[0].id)
    assert usage is not None and usage[0] >= 3000 and usage[1] == 0
