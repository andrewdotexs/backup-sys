from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from backupsys.catalog import db
from backupsys.config.models import Destination, Job, RetentionPolicy, Schedule
from backupsys.config.store import ConfigStore
from backupsys.core import restore as restore_mod
from backupsys.core.fsutil import source_key
from backupsys.scheduler.loop import Scheduler
from backupsys.service import BackupService, Task


def _setup(tmp_path: Path, schedule: Schedule | None = None) -> tuple[BackupService, Job, Destination, Path]:
    src = tmp_path / "pc"
    src.mkdir()
    (src / "doc.txt").write_text("contenuto originale")
    (src / "sub").mkdir()
    (src / "sub" / "x.txt").write_text("x")
    disk = tmp_path / "disk"
    disk.mkdir()
    svc = BackupService(ConfigStore())
    d = Destination(name="USB", path=str(disk), retention=RetentionPolicy(min_age_days=0))
    svc.save_destination(d)
    j = Job(name="Doc", sources=[str(src)], destinations=[d.id], schedule=schedule or Schedule())
    svc.save_job(j)
    return svc, j, d, src


def test_execute_backup_persists_volume_id(tmp_path: Path) -> None:
    svc, j, d, _ = _setup(tmp_path)
    res = svc.execute(Task("backup", j.id))
    assert res is not None and getattr(res, "status", None) == db.OK
    assert svc.config.destination(d.id).volume_id  # salvato in configurazione
    assert ConfigStore().load().destination(d.id).volume_id == svc.config.destination(d.id).volume_id


def test_queue_dedup(tmp_path: Path) -> None:
    svc, j, _d, _ = _setup(tmp_path)
    assert svc.enqueue_backup(j.id)
    assert not svc.enqueue_backup(j.id)
    assert svc.is_busy(j.id)


def test_worker_runs_queue(tmp_path: Path) -> None:
    import time

    svc, j, _d, _ = _setup(tmp_path)
    svc.start()
    try:
        svc.enqueue_backup(j.id)
        for _ in range(100):
            last = svc.catalog.last_run(j.id)
            if last and last.status != db.RUNNING and not svc.is_busy(j.id):
                break
            time.sleep(0.05)
        last = svc.catalog.last_run(j.id)
        assert last is not None and last.status == db.OK
    finally:
        svc.stop()


def test_verify_detects_corruption_and_repairs(tmp_path: Path) -> None:
    svc, j, d, src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    d = svc.config.destination(d.id)
    cur = Path(d.path) / "backup-sys" / j.slug / "current" / source_key(str(src))
    target = cur / "doc.txt"
    st = target.stat()
    data = bytearray(target.read_bytes())
    data[0] ^= 0xFF  # bit rot: stessa dimensione e data
    target.write_bytes(bytes(data))
    import os

    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    rep = svc.execute(Task("verify", j.id, dest_id=d.id))
    assert rep is not None and getattr(rep, "corrupted", None)
    # la verifica ha accodato una riparazione forzata
    queued = svc.queued()
    assert queued and queued[0].force_rels
    res = svc.execute(queued[0])
    assert getattr(res, "status", None) == db.OK
    assert target.read_text() == "contenuto originale"
    rep2 = svc.execute(Task("verify", j.id, dest_id=d.id))
    assert rep2 is not None and not getattr(rep2, "corrupted", True)
    assert svc.catalog.verifies()[0]["status"] == db.OK


def test_restore_never_overwrites(tmp_path: Path) -> None:
    svc, j, d, src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    (src / "doc.txt").write_text("modificato dopo il backup")
    rel = f"{source_key(str(src))}/doc.txt"
    rep = svc.restore(j.id, d.id, "current", rel, None)
    assert not rep.errors and len(rep.restored) == 1
    assert (src / "doc.txt").read_text() == "modificato dopo il backup"  # originale intatto
    restored = Path(rep.restored[0])
    assert restored.name.startswith("doc (ripristinato ") and restored.read_text() == "contenuto originale"


def test_restore_folder_to_other_location(tmp_path: Path) -> None:
    svc, j, d, src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    out = tmp_path / "ripristino"
    rep = svc.restore(j.id, d.id, "current", f"{source_key(str(src))}/sub", str(out))
    assert (out / "sub" / "x.txt").read_text() == "x" and not rep.errors


def test_browse_and_versions(tmp_path: Path) -> None:
    import os
    import time

    svc, j, d, src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    (src / "doc.txt").write_text("seconda versione!!")
    t = time.time() + 100
    os.utime(src / "doc.txt", (t, t))
    svc.execute(Task("backup", j.id))
    d = svc.config.destination(d.id)
    layout = restore_mod.job_layout(d, j)
    key = source_key(str(src))
    entries = restore_mod.browse(layout, "current", key)
    assert [e.name for e in entries] == ["sub", "doc.txt"]
    versions = restore_mod.file_versions(layout, f"{key}/doc.txt")
    assert [v.area.split("/")[0] for v in versions] == ["current", "versions"]
    assert restore_mod.original_location(j, f"{key}/sub/x.txt") == src / "sub" / "x.txt"


def test_browse_rejects_traversal(tmp_path: Path) -> None:
    import pytest

    svc, j, d, _src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    layout = restore_mod.job_layout(svc.config.destination(d.id), j)
    with pytest.raises(ValueError):
        restore_mod.browse(layout, "current", "../../..")
    with pytest.raises(ValueError):
        restore_mod.browse(layout, "versions/../../x", "")


def test_scheduler_runs_due_and_catches_up(tmp_path: Path) -> None:
    svc, _j, _d, _ = _setup(tmp_path, Schedule(cron="0 13 * * *"))
    sch = Scheduler(svc)
    now = datetime(2026, 11, 6, 12, 0)
    sch.run_once(now)  # prima volta: disco visto come pronto, mai sincronizzato => "indietro"
    assert [t.trigger for t in svc.queued()] == ["connect"]
    svc.execute(svc.queued()[0])
    svc._queue.clear()
    sch.run_once(now + timedelta(minutes=30))
    assert svc.queued() == []
    sch.run_once(datetime(2026, 11, 6, 13, 0, 1))
    assert [t.trigger for t in svc.queued()] == ["schedule"]


def test_scheduler_on_connect(tmp_path: Path) -> None:
    svc, _j, d, _ = _setup(tmp_path, Schedule(on_connect=True))
    disk = Path(svc.config.destination(d.id).path)
    sch = Scheduler(svc)
    hidden = disk.with_name("staccato")
    disk.rename(hidden)
    sch.run_once(datetime.now())
    assert svc.queued() == []
    hidden.rename(disk)
    sch.run_once(datetime.now())
    q = svc.queued()
    assert len(q) == 1 and q[0].trigger == "connect" and q[0].only_dest_ids == {d.id}


def test_remove_destination_keeps_data(tmp_path: Path) -> None:
    svc, j, d, _ = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    disk = Path(svc.config.destination(d.id).path)
    svc.remove_destination(d.id)
    assert (disk / "backup-sys").is_dir()
    assert svc.config.job(j.id).destinations == []


def test_scheduler_periodic_verify(tmp_path: Path) -> None:
    svc, j, _d, _ = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    sch = Scheduler(svc)
    sch.run_once(datetime.now())
    assert not [t for t in svc.queued() if t.kind == "verify"]
    sch.run_once(datetime.now() + timedelta(days=31))
    assert [t.kind for t in svc.queued()] == ["verify"]
