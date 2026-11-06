from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backupsys.config.store import ConfigStore
from backupsys.core.fsutil import source_key
from backupsys.scheduler.loop import Scheduler
from backupsys.service import BackupService
from backupsys.ui.app import create_app

PW = "password-di-prova"


@pytest.fixture
def svc() -> BackupService:
    return BackupService(ConfigStore())


@pytest.fixture
def client(svc: BackupService) -> TestClient:
    return TestClient(create_app(svc, Scheduler(svc)), follow_redirects=False)


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "token csrf mancante"
    return m.group(1)


def _login(client: TestClient) -> str:
    r = client.post("/setup", data={"password": PW, "password2": PW})
    assert r.status_code == 303
    r = client.get("/")
    assert r.status_code == 200
    return _csrf(r.text)


def test_redirects_to_setup_then_login(client: TestClient) -> None:
    assert client.get("/").headers["location"] == "/setup"
    _login(client)
    client.cookies.clear()
    assert client.get("/").headers["location"] == "/login"
    assert client.get("/api/status").status_code == 401


def test_login_wrong_password_and_lockout(client: TestClient) -> None:
    _login(client)
    client.cookies.clear()
    for _ in range(5):
        r = client.post("/login", data={"password": "sbagliata"})
        assert "err=" in r.headers["location"]
    r = client.post("/login", data={"password": PW})
    assert "Troppi" in client.get(r.headers["location"]).text or "tentativi" in r.headers["location"]


def test_security_headers_and_host_check(client: TestClient) -> None:
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/login", headers={"host": "evil.example.com"}).status_code == 400


def test_post_without_csrf_rejected(client: TestClient) -> None:
    _login(client)
    r = client.post("/jobs/save", data={"name": "x", "sources": "C:\\x"})
    assert r.status_code == 403


def test_full_flow(client: TestClient, svc: BackupService, tmp_path: Path) -> None:
    csrf = _login(client)
    disk = tmp_path / "usb"
    disk.mkdir()
    src = tmp_path / "Documenti"
    src.mkdir()
    (src / "a.txt").write_text("ciao")

    r = client.post(
        "/destinations/save",
        data={
            "csrf": csrf,
            "name": "USB",
            "path": str(disk),
            "max_versions_percent": "30",
            "max_backup_gb": "500",
            "min_age_days": "30",
        },
    )
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    dest = svc.config.destinations[0]
    assert dest.retention.max_backup_gb == 500

    r = client.post(
        "/jobs/save",
        data={
            "csrf": csrf,
            "name": "Documenti",
            "sources": str(src),
            "excludes": "*.tmp",
            "destinations": dest.id,
            "schedule_kind": "daily",
            "schedule_time": "13:30",
            "enabled": "on",
            "guard_threshold": "30",
            "guard_min_files": "50",
            "verify_every_days": "30",
        },
    )
    assert r.status_code == 303, r.text
    job = svc.config.jobs[0]
    assert job.schedule.cron == "30 13 * * *" and job.destinations == [dest.id]

    for page in [
        "/",
        "/jobs",
        f"/jobs/{job.id}",
        "/destinations",
        f"/destinations/{dest.id}",
        "/runs",
        "/browse",
        "/security",
        "/jobs/new",
        "/destinations/new",
    ]:
        assert client.get(page).status_code == 200, page

    # esecuzione (sincrona per il test)
    from backupsys.service import Task

    svc.execute(Task("backup", job.id))
    page = client.get("/")
    assert "Completato" in page.text
    run_id = svc.catalog.last_run(job.id).id  # type: ignore[union-attr]
    assert client.get(f"/runs/{run_id}").status_code == 200

    key = source_key(str(src))
    r = client.get(f"/browse?job={job.id}&dest={dest.id}&path={key}")
    assert r.status_code == 200 and "a.txt" in r.text
    r = client.get(f"/browse/versions?job={job.id}&dest={dest.id}&path={key}/a.txt")
    assert r.status_code == 200 and "Copia corrente" in r.text

    out = tmp_path / "out"
    r = client.post(
        "/restore",
        data={
            "csrf": csrf,
            "job": job.id,
            "dest": dest.id,
            "area": "current",
            "path": f"{key}/a.txt",
            "mode": "other",
            "target": str(out),
        },
    )
    assert r.status_code == 200 and (out / "a.txt").read_text() == "ciao"

    # avvio manuale dalla UI → in coda
    r = client.post(f"/jobs/{job.id}/run", data={"csrf": csrf})
    assert r.status_code == 303 and svc.is_busy(job.id)
    assert client.get("/api/status").json()["queued"] == [job.id]

    # rimozione job: i dati restano
    r = client.post(f"/jobs/{job.id}/delete", data={"csrf": csrf})
    assert r.status_code == 303 and svc.config.jobs == []
    assert (disk / "backup-sys").is_dir()


def test_invalid_job_shows_error(client: TestClient) -> None:
    csrf = _login(client)
    r = client.post("/jobs/save", data={"csrf": csrf, "name": "", "sources": "", "schedule_kind": "manual"})
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_encryption_setup_shows_recovery_code(client: TestClient, svc: BackupService, tmp_path: Path) -> None:
    csrf = _login(client)
    disk = tmp_path / "usb"
    disk.mkdir()
    client.post("/destinations/save", data={"csrf": csrf, "name": "USB", "path": str(disk)})
    dest = svc.config.destinations[0]
    r = client.post(
        f"/destinations/{dest.id}/encrypt",
        data={"csrf": csrf, "passphrase": "passphrase-lunga!", "passphrase2": "passphrase-lunga!"},
    )
    assert r.status_code == 200 and "Codice di recupero" in r.text
    assert svc.config.destination(dest.id).encrypted
    assert (disk / "backup-sys" / ".bsys-key.json").exists()


def test_change_password_logs_out(client: TestClient) -> None:
    csrf = _login(client)
    r = client.post(
        "/security/password", data={"csrf": csrf, "old": PW, "new": "nuova-password", "new2": "nuova-password"}
    )
    assert r.status_code == 303
    assert client.get("/").headers["location"] == "/login"
    r = client.post("/login", data={"password": "nuova-password"})
    assert r.headers["location"] == "/"


def test_job_save_resolves_link_and_warns_missing(client: TestClient, svc: BackupService, tmp_path: Path) -> None:
    import os

    csrf = _login(client)
    real = tmp_path / "Documents"
    real.mkdir()
    sources = [str(tmp_path / "manca")]
    if os.name != "nt":
        link = tmp_path / "Documenti"
        link.symlink_to(real, target_is_directory=True)
        sources.append(str(link))
    r = client.post(
        "/jobs/save",
        data={"csrf": csrf, "name": "Doc", "sources": "\n".join(sources), "schedule_kind": "manual"},
    )
    assert r.status_code == 303 and "salvato" in r.headers["location"] and "err=" in r.headers["location"]
    job = svc.config.jobs[0]
    if os.name != "nt":
        assert str(real) in job.sources and str(tmp_path / "Documenti") not in job.sources
    page = client.get(r.headers["location"])
    assert "non esiste" in page.text


def test_new_job_form_shows_known_folders(client: TestClient) -> None:
    _login(client)
    r = client.get("/jobs/new")
    assert r.status_code == 200
    textarea = r.text.split("<textarea", 1)[1].split(">", 1)[1].split("</textarea>")[0].strip()
    assert textarea != "C:\\Users"  # non più l'intera cartella utenti
    from backupsys.known_folders import known_folders

    known = known_folders()
    if known:  # su Windows: i Documenti reali (SHGetKnownFolderPath)
        assert textarea == known[0][1] and "data-add-source" in r.text


def test_damaged_installation_clear_error(svc: BackupService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from backupsys.ui import app as app_mod

    monkeypatch.setattr(app_mod, "HERE", tmp_path / "vuota")
    with pytest.raises(app_mod.InstallationDamagedError, match="Estrai di nuovo lo zip"):
        app_mod.create_app(svc)
