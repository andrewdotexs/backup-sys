"""Riga di comando: ``backup-sys daemon | run | status | verify | decrypt | autostart | reset-password``."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from backupsys import __version__

if TYPE_CHECKING:
    from backupsys.config.models import Job
    from backupsys.service import BackupService

app = typer.Typer(help="backup-sys: backup automatico append-only.", no_args_is_help=True, add_completion=False)
autostart_app = typer.Typer(help="Avvio automatico all'accesso a Windows.")
app.add_typer(autostart_app, name="autostart")


def _service() -> BackupService:
    from backupsys.service import BackupService

    return BackupService()


def _find_job(svc: BackupService, name: str) -> Job:
    for j in svc.config.jobs:
        if j.id == name or j.name.lower() == name.lower():
            return j
    typer.echo(f"Job «{name}» non trovato. Disponibili: {', '.join(j.name for j in svc.config.jobs) or 'nessuno'}")
    raise typer.Exit(2)


@app.command()
def version() -> None:
    """Mostra la versione."""
    typer.echo(__version__)


@app.command()
def daemon(
    port: int = typer.Option(0, help="Porta dell'interfaccia (default da configurazione: 8765)"),
    no_tray: bool = typer.Option(False, "--no-tray", help="Senza icona nella tray"),
    open_browser: bool = typer.Option(False, "--open", help="Apri il browser all'avvio"),
) -> None:
    """Avvia il servizio in background con interfaccia web e scheduler."""
    from backupsys import daemon as d

    d.run(port or None, tray=not no_tray, open_browser=open_browser)


@app.command()
def run(
    job: str = typer.Argument(..., help="Nome o id del job"),
    confirm: bool = typer.Option(False, "--confirm", help="Conferma un'esecuzione messa in pausa dal guard"),
) -> None:
    """Esegue subito un backup (in primo piano)."""
    from backupsys.catalog import db
    from backupsys.core.engine import RunResult
    from backupsys.daemon import setup_logging
    from backupsys.service import Task

    setup_logging()
    svc = _service()
    j = _find_job(svc, job)
    res = svc.execute(Task("backup", j.id, "confirm" if confirm else "manual", override_guard=confirm))
    if not isinstance(res, RunResult):
        raise typer.Exit(1)
    for d in res.dests:
        typer.echo(
            f"  {d.name}: {d.status} — nuovi {d.copied}, versioni {d.versioned}, invariati {d.skipped}, "
            f"errori {len(d.errors)} {d.message}"
        )
    typer.echo(f"Esito: {res.status}")
    raise typer.Exit(0 if res.status in (db.OK, db.WARNING) else 1)


@app.command()
def status() -> None:
    """Stato dei job e dei dischi."""
    from backupsys.core.fsutil import human_bytes
    from backupsys.scheduler.timing import describe

    svc = _service()
    cfg = svc.config
    typer.echo("Dischi:")
    for d in cfg.destinations:
        i = svc.dest_info(d)
        space = f" — liberi {human_bytes(i.free)} su {human_bytes(i.total)}" if i.total else ""
        typer.echo(f"  [{'OK' if i.status.ready else '--'}] {d.name} ({d.path}): {i.status.message}{space}")
    typer.echo("Backup:")
    for j in cfg.jobs:
        last = svc.catalog.last_run(j.id)
        ls = f"{last.status} {last.started_at}" if last else "mai eseguito"
        typer.echo(f"  {j.name}: {describe(j.schedule)} — ultimo: {ls}")


@app.command()
def verify(job: str = typer.Argument(..., help="Nome o id del job")) -> None:
    """Verifica l'integrità dei backup di un job su tutti i suoi dischi."""
    from backupsys.core.verify import VerifyReport
    from backupsys.service import Task

    svc = _service()
    j = _find_job(svc, job)
    bad = False
    for dest_id in j.destinations:
        rep = svc.execute(Task("verify", j.id, dest_id=dest_id))
        name = svc.config.destination(dest_id).name
        if not isinstance(rep, VerifyReport):
            typer.echo(f"  {name}: non verificabile (disco non collegato?)")
            continue
        typer.echo(
            f"  {name}: {rep.ok}/{rep.checked} integri, {len(rep.corrupted)} corrotti, {len(rep.missing)} mancanti"
        )
        bad = bad or bool(rep.corrupted)
    raise typer.Exit(1 if bad else 0)


@app.command()
def decrypt(
    source: Path = typer.Argument(..., help="File .bsenc o cartella del backup cifrato"),
    target: Path = typer.Argument(..., help="Cartella in cui scrivere i file decifrati"),
    code: str = typer.Option("", help="Codice di recupero (in alternativa alla passphrase)"),
    keyfile: Path = typer.Option(None, help="File .bsys-key.json del disco (con --passphrase)"),
) -> None:
    """Decifra file di backup senza bisogno dell'interfaccia (recupero d'emergenza)."""
    from backupsys.security import crypto

    if code:
        key = crypto.key_from_recovery_code(code)
    else:
        kf = keyfile or _find_keyfile(source)
        if kf is None:
            typer.echo("Indica --code oppure --keyfile")
            raise typer.Exit(2)
        key = crypto.unlock_keyfile(kf, typer.prompt("Passphrase", hide_input=True))
    cipher = crypto.StreamCipher(key)
    files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
    n = 0
    for f in files:
        if not f.name.endswith(crypto.SUFFIX):
            continue
        rel = f.relative_to(source) if source.is_dir() else Path(f.name)
        out = target / rel.parent / f.name[: -len(crypto.SUFFIX)]
        out.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with f.open("rb") as fin:
            cipher.decrypt_stream(fin, buf)
        with open(out, "xb") as fout:  # mai sovrascrivere
            fout.write(buf.getvalue())
        n += 1
    typer.echo(f"{n} file decifrati in {target}")


def _find_keyfile(p: Path) -> Path | None:
    from backupsys.security import crypto

    for parent in [p, *p.parents]:
        cand = parent / crypto.KEYFILE_NAME
        if cand.exists():
            return cand
    return None


@app.command("reset-password")
def reset_password() -> None:
    """Imposta una nuova password dell'interfaccia (serve accesso a questo PC)."""
    from backupsys.security.audit import AuditLog
    from backupsys.security.auth import AuthStore

    pw = typer.prompt("Nuova password", hide_input=True, confirmation_prompt=True)
    AuthStore().set_password(pw)
    AuditLog().log("password_reset_cli", actor="user")
    typer.echo("Password aggiornata.")


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", help="Non chiedere conferma"),
    keep_program: bool = typer.Option(False, "--keep-program", help="Rimuovi solo i dati, non i file del programma"),
) -> None:
    """Disinstalla backup-sys e rimuove i dati locali. I dischi di backup NON vengono toccati."""
    from backupsys import uninstall as u

    frozen = getattr(sys, "frozen", False)

    def pause() -> None:
        if frozen and not yes:  # la finestra della console si chiuderebbe subito
            typer.prompt("Premi Invio per chiudere", default="", show_default=False)

    plan = u.build_plan(include_program=not keep_program)
    typer.echo("Disinstallazione di backup-sys\n")
    for line in u.describe(plan):
        typer.echo(line)
    if plan.problems:
        typer.echo("\nDisinstallazione bloccata per sicurezza:")
        for pr in plan.problems:
            typer.echo(f"  - {pr}")
        pause()
        raise typer.Exit(2)
    if not yes:
        answer = typer.prompt("\nPer confermare scrivi RIMUOVI", default="", show_default=False)
        if answer.strip().upper() != "RIMUOVI":
            typer.echo("Annullato: nulla è stato rimosso.")
            pause()
            raise typer.Exit(1)
    errors = u.execute(plan)
    if errors:
        typer.echo("\nCompletato con problemi:")
        for e in errors:
            typer.echo(f"  - {e}")
        pause()
        raise typer.Exit(1)
    typer.echo("\nbackup-sys è stato disinstallato. I dischi di backup sono intatti.")
    if plan.program_dir is not None:
        typer.echo("I file del programma verranno eliminati tra qualche secondo, dopo la chiusura di questa finestra.")
    elif not keep_program:
        typer.echo("Installazione da sorgente: elimina a mano la cartella del progetto se non ti serve più.")
    pause()


@autostart_app.command("on")
def autostart_on() -> None:
    from backupsys.scheduler import autostart

    typer.echo(f"Avvio automatico attivato: {autostart.install()}")


@autostart_app.command("off")
def autostart_off() -> None:
    from backupsys.scheduler import autostart

    autostart.uninstall()
    typer.echo("Avvio automatico disattivato.")


def main() -> None:  # pragma: no cover
    if os.name == "nt" and sys.stdout is None:  # avviato con pythonw: niente console
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        sys.stderr = sys.stdout
    app()
