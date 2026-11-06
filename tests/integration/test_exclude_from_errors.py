from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupsys.catalog import db
from backupsys.config.models import Destination, Job, RetentionPolicy
from backupsys.config.store import ConfigStore
from backupsys.core.scanner import Excluder, scan
from backupsys.service import BackupService, Task
from backupsys.ui.app import create_app


def test_excluder_absolute_paths(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "posta").mkdir(parents=True)
    (src / "posta" / "archivio.pst").write_text("x")
    (src / "altro" / "archivio.pst").parent.mkdir()
    (src / "altro" / "archivio.pst").write_text("y")  # stesso nome altrove: NON escluso
    (src / "cache").mkdir()
    (src / "cache" / "c.bin").write_text("z")
    (src / "tenere.txt").write_text("ok")
    res = scan([str(src)], [str(src / "posta" / "archivio.pst"), str(src / "cache")])
    assert sorted(f.rel for f in res.files) == ["altro/archivio.pst", "tenere.txt"]
    ex = Excluder([str(src / "cache"), "*.tmp"])
    assert ex.path_excluded(str(src / "cache")) and not ex.path_excluded(str(src / "tenere.txt"))
    assert ex.file_excluded("a.tmp")


def _setup(tmp_path: Path) -> tuple[BackupService, Job, Path]:
    src = tmp_path / "pc"
    src.mkdir()
    (src / "ok.txt").write_text("ok")
    (src / "bloccato.pst").write_text("in uso")
    disk = tmp_path / "usb"
    disk.mkdir()
    svc = BackupService(ConfigStore())
    d = Destination(name="USB", path=str(disk), retention=RetentionPolicy(min_age_days=0))
    svc.save_destination(d)
    j = Job(name="Doc", sources=[str(src)], destinations=[d.id])
    svc.save_job(j)
    return svc, j, src


def test_exclude_path_rules(tmp_path: Path) -> None:
    svc, j, src = _setup(tmp_path)
    saved = svc.exclude_path(j.id, str(src / "bloccato.pst"))
    assert saved in svc.config.job(j.id).excludes
    svc.exclude_path(j.id, str(src / "bloccato.pst"))  # idempotente
    assert svc.config.job(j.id).excludes.count(saved) == 1
    with pytest.raises(ValueError, match="cartella sorgente"):
        svc.exclude_path(j.id, str(src))
    with pytest.raises(ValueError, match="non appartiene"):
        svc.exclude_path(j.id, str(tmp_path / "altrove" / "x.txt"))
    with pytest.raises(ValueError):
        svc.exclude_path(j.id, "relativo/x.txt")
    assert any(e["action"] == "job_exclude_added" for e in svc.audit.entries())


def test_exclude_button_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, j, _src = _setup(tmp_path)
    import builtins

    real_open = builtins.open

    def locked(file, mode="r", *a, **k):  # type: ignore[no-untyped-def]
        if str(file).endswith("bloccato.pst") and "r" in mode and "usb" not in str(file):
            raise PermissionError(13, "Il file è in uso da un altro processo")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", locked)
    r1 = svc.execute(Task("backup", j.id))
    monkeypatch.setattr(builtins, "open", real_open)
    assert getattr(r1, "status", None) == db.WARNING

    client = TestClient(create_app(svc), follow_redirects=False)
    client.post("/setup", data={"password": "password-x", "password2": "password-x"})
    run_id = svc.catalog.last_run(j.id).id  # type: ignore[union-attr]
    page = client.get(f"/runs/{run_id}").text
    assert "Escludi dai prossimi backup" in page and "\\\\?\\" not in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    path = re.search(r'name="path" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    assert path.endswith("bloccato.pst")
    r = client.post(f"/runs/{run_id}/exclude", data={"csrf": csrf, "path": path})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert "escluso" in client.get(f"/runs/{run_id}").text

    monkeypatch.setattr(builtins, "open", locked)
    r2 = svc.execute(Task("backup", j.id))  # il file resta bloccato, ma ora è escluso: nessun avviso
    monkeypatch.setattr(builtins, "open", real_open)
    assert getattr(r2, "status", None) == db.OK


def test_exclude_requires_csrf(tmp_path: Path) -> None:
    svc, j, src = _setup(tmp_path)
    svc.execute(Task("backup", j.id))
    client = TestClient(create_app(svc), follow_redirects=False)
    client.post("/setup", data={"password": "password-x", "password2": "password-x"})
    run_id = svc.catalog.last_run(j.id).id  # type: ignore[union-attr]
    r = client.post(f"/runs/{run_id}/exclude", data={"path": str(src / "ok.txt")})
    assert r.status_code == 403 and svc.config.job(j.id).excludes == j.excludes
