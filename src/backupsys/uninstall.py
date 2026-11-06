"""Disinstallazione: rimuove programma e dati locali, MAI i dischi di backup.

Cosa viene rimosso:
- configurazione (``%APPDATA%\\backup-sys``) e dati locali (``%LOCALAPPDATA%\\backup-sys``: catalogo,
  audit log, log, lock);
- i segreti nel Gestore credenziali di Windows (password di rete, chiavi di cifratura, chiave dell'audit log);
- l'avvio automatico;
- i file del programma (solo quelli che gli appartengono: ``backup-sys*.exe`` e ``_internal``).

Regole di sicurezza:
- prima si chiude il servizio in esecuzione (altrimenti catalogo e log restano bloccati);
- si cancellano solo le cartelle dati esatte di backup-sys, e mai se una destinazione di backup
  ci sta dentro (o viceversa);
- nella cartella del programma si rimuovono solo i file noti; la cartella stessa solo se poi è vuota.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from backupsys import APP_NAME, appdirs, localhttp
from backupsys.config.store import ConfigStore
from backupsys.core.fsutil import is_relative_to
from backupsys.scheduler import autostart
from backupsys.security import audit, secrets

PROGRAM_FILES = ("backup-sys.exe", "backup-sys-cli.exe", "backup-sys-uninstall.exe")
PROGRAM_DIRS = ("_internal",)


class UninstallSafetyError(Exception):
    pass


@dataclass
class UninstallPlan:
    data_dirs: list[Path] = field(default_factory=list)
    secret_names: list[str] = field(default_factory=list)
    autostart: bool = False
    program_dir: Path | None = None
    program_items: list[Path] = field(default_factory=list)
    destinations: list[tuple[str, str]] = field(default_factory=list)  # (nome, percorso): NON toccati
    encrypted_destinations: list[str] = field(default_factory=list)
    port: int = 8765
    problems: list[str] = field(default_factory=list)  # motivi per cui la disinstallazione è bloccata


def _program_dir() -> Path | None:
    """Cartella dell'eseguibile, solo per la versione installata (PyInstaller)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def program_items(folder: Path) -> list[Path]:
    """Elementi del programma presenti in ``folder`` (confronto senza distinzione maiuscole/minuscole)."""
    wanted = {n.lower() for n in (*PROGRAM_FILES, *PROGRAM_DIRS)}
    try:
        return sorted(p for p in folder.iterdir() if p.name.lower() in wanted)
    except OSError:
        return []


def _forbidden_dir(p: Path) -> bool:
    """Cartelle che non devono MAI essere cancellate per intero."""
    p = p.resolve()
    home = Path.home().resolve()
    return p == home or p.parent == p or p == home.parent


def build_plan(include_program: bool = True) -> UninstallPlan:
    plan = UninstallPlan()
    try:
        cfg = ConfigStore().load()
    except Exception:
        cfg = None
        plan.problems.append("Configurazione illeggibile: non posso elencare i segreti delle destinazioni")
    if cfg is not None:
        plan.port = cfg.ui.port
        plan.destinations = [(d.name, d.path) for d in cfg.destinations]
        plan.encrypted_destinations = [d.name for d in cfg.destinations if d.encrypted]
        for d in cfg.destinations:
            plan.secret_names += [secrets.network_password_name(d.id), secrets.dest_key_name(d.id)]
    plan.secret_names.append(audit.KEY_NAME)

    for local_dir in (appdirs.config_dir(), appdirs.data_dir()):
        ddir = local_dir.resolve()
        if _forbidden_dir(ddir):
            plan.problems.append(f"Cartella dati non sicura da rimuovere: {ddir}")
            continue
        for _name, dpath in plan.destinations:
            dp = Path(os.path.abspath(dpath))
            if is_relative_to(dp, ddir) or is_relative_to(ddir, dp):
                plan.problems.append(
                    f"La destinazione di backup «{dpath}» si sovrappone alla cartella dati «{ddir}»: "
                    "per sicurezza non rimuovo nulla automaticamente"
                )
        plan.data_dirs.append(ddir)

    plan.autostart = autostart.is_installed()

    if include_program:
        folder = _program_dir()
        if folder is not None:
            plan.program_dir = folder
            plan.program_items = program_items(folder)
            for _name, dpath in plan.destinations:
                if is_relative_to(Path(os.path.abspath(dpath)), folder):
                    plan.problems.append(f"La destinazione «{dpath}» è dentro la cartella del programma")
    return plan


def stop_running_daemon(port: int, timeout: float = 15) -> bool:
    """Chiude il servizio in ascolto su ``port`` (se è davvero backup-sys). True se nessuno è più attivo."""
    info = localhttp.healthz(port)
    if info is None:
        return True  # nessun servizio attivo
    pid = info.get("pid")
    if not info.get("ok") or not isinstance(pid, int) or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)  # su Windows: TerminateProcess (le copie a metà restano .bsys-partial)
    except OSError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if localhttp.healthz(port, timeout=1) is None:
            time.sleep(1)  # lascia al sistema il tempo di chiudere i file
            return True
        time.sleep(0.5)
    return False


def _remove_tree(d: Path, allowed: list[Path]) -> None:
    d = d.resolve()
    if d not in [a.resolve() for a in allowed] or _forbidden_dir(d):
        raise UninstallSafetyError(f"Rifiuto di rimuovere {d}")
    if d.exists():
        shutil.rmtree(d)  # cartella dati locale di backup-sys (ammesso dal test anti-cancellazione)


def execute(plan: UninstallPlan) -> list[str]:
    """Esegue il piano. Restituisce gli errori (vuoto = tutto rimosso)."""
    if plan.problems:
        raise UninstallSafetyError("; ".join(plan.problems))
    errors: list[str] = []
    if not stop_running_daemon(plan.port):
        errors.append("Non sono riuscito a chiudere backup-sys in esecuzione: chiudilo dalla tray e riprova")
        return errors
    if plan.autostart:
        try:
            autostart.uninstall()
        except OSError as e:
            errors.append(f"Avvio automatico: {e}")
    for name in plan.secret_names:
        try:
            secrets.forget_secret(name)
        except Exception as e:  # keyring può sollevare eccezioni di vario tipo
            errors.append(f"Credenziale {name}: {e}")
    for d in plan.data_dirs:
        try:
            _remove_tree(d, plan.data_dirs)
        except OSError as e:
            errors.append(f"{d}: {e}")
    if plan.program_dir is not None and plan.program_items:
        try:
            schedule_program_removal(plan.program_dir, plan.program_items)
        except OSError as e:
            errors.append(f"Programma: {e}")
    return errors


def removal_command(folder: Path, items: list[Path]) -> str:
    """Comando cmd.exe che, dopo qualche secondo (il disinstallatore deve chiudersi), rimuove i file del
    programma e poi la cartella solo se è rimasta vuota (``rmdir`` senza /s)."""
    steps = ["ping 127.0.0.1 -n 4 >nul"]
    for p in items:
        if p.name.lower() in PROGRAM_DIRS:
            steps.append(f'rmdir /s /q "{p}"')
        else:
            steps.append(f'del /f /q "{p}"')
    steps.append(f'rmdir "{folder}"')
    return " & ".join(steps)


def schedule_program_removal(folder: Path, items: list[Path]) -> None:  # pragma: no cover - solo Windows
    """Un eseguibile in uso non si può cancellare: la rimozione la fa un cmd.exe staccato dopo la chiusura."""
    if sys.platform != "win32":
        return
    for p in items:  # ultimo controllo: solo elementi noti, dentro la cartella del programma
        if p.parent.resolve() != folder.resolve() or p.name.lower() not in {
            n.lower() for n in (*PROGRAM_FILES, *PROGRAM_DIRS)
        }:
            raise UninstallSafetyError(f"Elemento inatteso: {p}")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        f'cmd.exe /c "{removal_command(folder, items)}"',
        creationflags=flags,
        close_fds=True,
        cwd=os.environ.get("TEMP", "C:\\"),
    )


def describe(plan: UninstallPlan) -> list[str]:
    """Riepilogo leggibile di ciò che verrà fatto."""
    out = ["Verranno rimossi:"]
    out += [f"  - {d}" for d in plan.data_dirs]
    out.append(f"  - {len(plan.secret_names)} credenziali di {APP_NAME} nel Gestore credenziali di Windows")
    if plan.autostart:
        out.append("  - l'avvio automatico all'accesso")
    if plan.program_dir is not None:
        out.append(f"  - i file del programma in {plan.program_dir} ({len(plan.program_items)} elementi)")
    out.append("")
    out.append("NON verranno toccati i dischi di backup:")
    out += [f"  - {n}: {p}" for n, p in plan.destinations] or ["  (nessuno configurato)"]
    if plan.encrypted_destinations:
        out.append("")
        out.append(
            "ATTENZIONE: questi dischi sono cifrati: " + ", ".join(plan.encrypted_destinations) + ". "
            "Dopo la disinstallazione potrai leggerli solo con la passphrase o il codice di recupero."
        )
    return out
