from __future__ import annotations

from pathlib import Path

import pytest

from backupsys import appdirs, uninstall
from backupsys.config.models import Destination
from backupsys.config.store import ConfigStore
from backupsys.security import secrets
from backupsys.security.audit import AuditLog
from backupsys.service import BackupService, Task
from tests.conftest import Env


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(uninstall, "stop_running_daemon", lambda port, timeout=15: True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # avvio automatico (Linux) isolato


def test_removes_local_data_secrets_and_autostart_but_not_backups(env: Env) -> None:
    env.write("a.txt", "dato prezioso")
    svc = BackupService(ConfigStore())
    for d in env.config.destinations:
        svc.save_destination(d)
    svc.save_job(env.job)
    svc.execute(Task("backup", env.job.id))
    dest = svc.config.destinations[0]
    svc.set_network_password(dest.id, "pw-di-rete")
    AuditLog().log("prova")
    from backupsys.scheduler import autostart

    autostart.install()
    assert autostart.is_installed()

    plan = uninstall.build_plan(include_program=True)
    assert not plan.problems
    assert plan.program_dir is None  # non è l'eseguibile installato
    assert set(plan.data_dirs) == {appdirs.config_dir().resolve(), appdirs.data_dir().resolve()}
    text = "\n".join(uninstall.describe(plan))
    assert "NON verranno toccati" in text and dest.path in text

    data_dirs = list(plan.data_dirs)
    assert uninstall.execute(plan) == []
    assert all(not d.exists() for d in data_dirs)
    assert secrets.get_secret(secrets.network_password_name(dest.id)) is None
    assert secrets.get_secret("audit-hmac-key") is None
    assert not autostart.is_installed()
    # il backup sul disco è intatto
    assert (env.current() / "a.txt").read_text() == "dato prezioso"
    assert (env.disks[0] / "backup-sys" / ".bsys-volume.json").exists()


def test_warns_about_encrypted_disks(env: Env) -> None:
    store = ConfigStore()
    cfg = env.config.model_copy(deep=True)
    cfg.destinations[0].encrypted = True
    store.save(cfg)
    text = "\n".join(uninstall.describe(uninstall.build_plan()))
    assert "cifrati" in text and "codice di recupero" in text


def test_refuses_when_destination_overlaps_data_dir() -> None:
    store = ConfigStore()
    from backupsys.config.models import AppConfig

    store.save(AppConfig(destinations=[Destination(name="strana", path=str(appdirs.data_dir() / "bk"))]))
    plan = uninstall.build_plan()
    assert plan.problems and "si sovrappone" in plan.problems[0]
    with pytest.raises(uninstall.UninstallSafetyError):
        uninstall.execute(plan)
    assert appdirs.data_dir().exists()


def test_stops_if_daemon_cannot_be_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uninstall, "stop_running_daemon", lambda port, timeout=15: False)
    plan = uninstall.build_plan()
    errors = uninstall.execute(plan)
    assert errors and "chiudilo dalla tray" in errors[0]
    assert appdirs.config_dir().exists()


def test_program_items_only_known_files(tmp_path: Path) -> None:
    folder = tmp_path / "Desktop"
    (folder / "_internal").mkdir(parents=True)
    for n in ("backup-sys.exe", "backup-sys-cli.exe", "Backup-Sys-Uninstall.EXE", "tesi.docx", "foto.jpg"):
        (folder / n).write_text("x")
    items = uninstall.program_items(folder)
    assert sorted(p.name for p in items) == sorted(
        ["_internal", "backup-sys.exe", "backup-sys-cli.exe", "Backup-Sys-Uninstall.EXE"]
    )
    cmd = uninstall.removal_command(folder, items)
    assert "tesi.docx" not in cmd and "foto.jpg" not in cmd
    assert f'rmdir /s /q "{folder / "_internal"}"' in cmd
    assert cmd.endswith(f'rmdir "{folder}"')  # senza /s: la cartella sparisce solo se è vuota


def test_cli_cancel_without_confirmation() -> None:
    from typer.testing import CliRunner

    from backupsys.cli import app

    r = CliRunner().invoke(app, ["uninstall"], input="no\n")
    assert r.exit_code == 1 and "Annullato" in r.output
    assert appdirs.config_dir().exists()


def test_cli_confirmed() -> None:
    from typer.testing import CliRunner

    from backupsys.cli import app

    cfg_dir = appdirs.config_dir()
    r = CliRunner().invoke(app, ["uninstall"], input="RIMUOVI\n")
    assert r.exit_code == 0, r.output
    assert "intatti" in r.output and not cfg_dir.exists()
