"""Servizio applicativo: punto unico usato da interfaccia web, CLI, scheduler e tray.

Le esecuzioni (backup e verifiche) passano da una coda e vengono eseguite una alla volta da un
thread worker a bassa priorità, per non rallentare il PC.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backupsys.catalog import db
from backupsys.catalog.db import Catalog
from backupsys.config.models import AppConfig, Destination, Job
from backupsys.config.store import ConfigStore
from backupsys.core import restore as restore_mod
from backupsys.core import verify as verify_mod
from backupsys.core.engine import BackupEngine, Progress, RunOptions, RunResult
from backupsys.core.fsutil import disk_usage, human_bytes
from backupsys.core.layout import DestLayout
from backupsys.core.retention import tree_size
from backupsys.destinations import base as destinations
from backupsys.security import crypto, secrets
from backupsys.security.audit import AuditLog

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], None]


@dataclass
class Task:
    kind: str  # "backup" | "verify"
    job_id: str
    trigger: str = "manual"
    override_guard: bool = False
    only_dest_ids: set[str] | None = None
    dest_id: str | None = None  # per le verifiche
    force_rels: dict[str, set[str]] = field(default_factory=dict)
    queued_at: float = field(default_factory=time.time)


@dataclass
class Running:
    task: Task
    job_name: str
    started: float
    cancel: threading.Event
    progress: Progress | None = None
    label: str = ""


@dataclass
class DestInfo:
    dest: Destination
    status: destinations.DestStatus
    total: int = 0
    free: int = 0
    versions_bytes: int = 0

    @property
    def used_percent(self) -> float:
        return 100 * (self.total - self.free) / self.total if self.total else 0.0

    @property
    def versions_percent(self) -> float:
        return 100 * self.versions_bytes / self.total if self.total else 0.0


def _background_priority() -> None:
    """Abbassa priorità CPU e I/O del thread worker (Windows: THREAD_MODE_BACKGROUND_BEGIN)."""
    if os.name == "nt":  # pragma: no cover - solo Windows
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.SetThreadPriority(k32.GetCurrentThread(), 0x00010000)


class BackupService:
    def __init__(
        self,
        store: ConfigStore | None = None,
        catalog: Catalog | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.store = store or ConfigStore()
        self.catalog = catalog or Catalog()
        self.audit = audit or AuditLog()
        self._cfg_lock = threading.RLock()
        self._config = self.store.load()
        self.engine = BackupEngine(self.catalog, self.audit, on_bind=self._on_bind)
        self._queue: list[Task] = []
        self._qcond = threading.Condition()
        self.running: Running | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.notifier: Notifier = lambda title, msg: log.info("%s: %s", title, msg)
        self._dest_cache: dict[str, tuple[float, DestInfo]] = {}

    # ------------------------------------------------------------------ configurazione
    @property
    def config(self) -> AppConfig:
        with self._cfg_lock:
            return self._config.model_copy(deep=True)

    def mutate(self, fn: Callable[[AppConfig], None], action: str, actor: str = "user", **details: object) -> None:
        with self._cfg_lock:
            cfg = self._config.model_copy(deep=True)
            fn(cfg)
            cfg = AppConfig.model_validate(cfg.model_dump())  # rivalida
            self.store.save(cfg)
            self._config = cfg
        self._dest_cache.clear()
        self.audit.log(action, actor=actor, **details)

    def _on_bind(self, dest_id: str, volume_id: str) -> None:
        def fn(c: AppConfig) -> None:
            c.destination(dest_id).volume_id = volume_id

        self.mutate(fn, "config_dest_bound", actor="system", dest_id=dest_id)

    def save_job(self, job: Job) -> None:
        def fn(c: AppConfig) -> None:
            c.jobs = [j for j in c.jobs if j.id != job.id] + [job]
            c.jobs.sort(key=lambda j: j.name.lower())

        self.mutate(fn, "config_job_saved", job=job.name, job_id=job.id)

    def remove_job(self, job_id: str) -> None:
        """Rimuove il job dalla configurazione. I dati sui dischi di backup NON vengono toccati."""
        name = self.config.job(job_id).name

        def fn(c: AppConfig) -> None:
            c.jobs = [j for j in c.jobs if j.id != job_id]

        self.mutate(fn, "config_job_removed", job=name, job_id=job_id)

    def save_destination(self, dest: Destination) -> None:
        def fn(c: AppConfig) -> None:
            c.destinations = [d for d in c.destinations if d.id != dest.id] + [dest]
            c.destinations.sort(key=lambda d: d.name.lower())

        self.mutate(fn, "config_dest_saved", dest=dest.name, dest_id=dest.id)

    def remove_destination(self, dest_id: str) -> None:
        """Rimuove la destinazione dalla configurazione. Il contenuto del disco NON viene toccato."""
        name = self.config.destination(dest_id).name

        def fn(c: AppConfig) -> None:
            c.destinations = [d for d in c.destinations if d.id != dest_id]
            for j in c.jobs:
                j.destinations = [x for x in j.destinations if x != dest_id]

        self.mutate(fn, "config_dest_removed", dest=name, dest_id=dest_id)

    def rebind_destination(self, dest_id: str) -> None:
        """Associa la destinazione al disco attualmente collegato (azione esplicita dell'utente)."""

        def fn(c: AppConfig) -> None:
            d = c.destination(dest_id)
            d.volume_id = None
            d.volume_id = destinations.bind(d)

        self.mutate(fn, "dest_rebound", dest_id=dest_id)

    # ------------------------------------------------------------------ cifratura
    def setup_encryption(self, dest_id: str, passphrase: str) -> str:
        """Crea la chiave sul disco e la salva nel Credential Manager. Restituisce il codice di recupero."""
        dest = self.config.destination(dest_id)
        layout = DestLayout(Path(dest.path))
        if not os.path.isdir(dest.path):
            raise FileNotFoundError("Collega il disco prima di attivare la cifratura")
        keyfile = layout.root / crypto.KEYFILE_NAME
        if keyfile.exists():
            key = crypto.unlock_keyfile(keyfile, passphrase)
        else:
            if layout.job_roots():
                raise ValueError("Il disco contiene già backup in chiaro: usa un disco (o cartella) vuoto")
            key = crypto.create_keyfile(keyfile, passphrase)
        secrets.set_dest_key(dest_id, key)

        def fn(c: AppConfig) -> None:
            c.destination(dest_id).encrypted = True

        self.mutate(fn, "dest_encryption_enabled", dest=dest.name)
        return crypto.recovery_code(key)

    def unlock_destination(self, dest_id: str, passphrase: str) -> None:
        dest = self.config.destination(dest_id)
        keyfile = DestLayout(Path(dest.path)).root / crypto.KEYFILE_NAME
        key = crypto.unlock_keyfile(keyfile, passphrase)
        secrets.set_dest_key(dest_id, key)
        self.audit.log("dest_unlocked", actor="user", dest=dest.name)

    def set_network_password(self, dest_id: str, password: str) -> None:
        secrets.set_secret(secrets.network_password_name(dest_id), password)
        self.audit.log("dest_network_credentials_set", actor="user", dest_id=dest_id)

    # ------------------------------------------------------------------ informazioni destinazioni
    def dest_info(self, dest: Destination, max_age: float = 60) -> DestInfo:
        cached = self._dest_cache.get(dest.id)
        if cached and time.time() - cached[0] < max_age and cached[1].dest == dest:
            return cached[1]
        status = destinations.check(dest)
        info = DestInfo(dest, status)
        if status.available:
            try:
                info.total, _u, info.free = disk_usage(dest.path)
                layout = DestLayout(Path(dest.path))
                info.versions_bytes = sum(tree_size(j / "versions") for j in layout.job_roots())
            except OSError:
                pass
        self._dest_cache[dest.id] = (time.time(), info)
        return info

    # ------------------------------------------------------------------ coda
    def enqueue(self, task: Task) -> bool:
        with self._qcond:
            dup = any(
                t.kind == task.kind and t.job_id == task.job_id and t.dest_id == task.dest_id for t in self._queue
            )
            if self.running and self.running.task.kind == task.kind and self.running.task.job_id == task.job_id:
                dup = dup or task.trigger in ("schedule", "connect")
            if dup:
                return False
            self._queue.append(task)
            self._qcond.notify()
            return True

    def enqueue_backup(
        self,
        job_id: str,
        trigger: str = "manual",
        override_guard: bool = False,
        only_dest_ids: set[str] | None = None,
    ) -> bool:
        return self.enqueue(Task("backup", job_id, trigger, override_guard, only_dest_ids))

    def enqueue_verify(self, job_id: str, dest_id: str, trigger: str = "manual") -> bool:
        return self.enqueue(Task("verify", job_id, trigger, dest_id=dest_id))

    def queued(self) -> list[Task]:
        with self._qcond:
            return list(self._queue)

    def is_busy(self, job_id: str) -> bool:
        r = self.running
        return bool((r and r.task.job_id == job_id) or any(t.job_id == job_id for t in self.queued()))

    def cancel_running(self) -> None:
        r = self.running
        if r:
            r.cancel.set()
            self.audit.log("task_cancelled", actor="user", job=r.job_name, kind=r.task.kind)

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._work, name="backup-worker", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self.cancel_running()
        with self._qcond:
            self._qcond.notify_all()
        if self._worker:
            self._worker.join(timeout)

    def _work(self) -> None:
        _background_priority()
        while not self._stop.is_set():
            with self._qcond:
                while not self._queue and not self._stop.is_set():
                    self._qcond.wait(timeout=1)
                if self._stop.is_set():
                    return
                task = self._queue.pop(0)
            try:
                self.execute(task)
            except Exception:  # il worker non deve mai morire
                log.exception("Esecuzione fallita: %s", task)

    def execute(self, task: Task) -> RunResult | verify_mod.VerifyReport | None:
        """Esegue subito un task (usato dal worker; dalla CLI per esecuzioni sincrone)."""
        cfg = self.config
        try:
            job = cfg.job(task.job_id)
        except KeyError:
            return None
        running = Running(task, job.name, time.time(), threading.Event())
        self.running = running
        try:
            if task.kind == "backup":
                return self._do_backup(cfg, job, task, running)
            return self._do_verify(cfg, job, task, running)
        finally:
            self.running = None
            self._dest_cache.clear()

    def _do_backup(self, cfg: AppConfig, job: Job, task: Task, running: Running) -> RunResult:
        dests = [cfg.destination(d) for d in job.destinations if any(x.id == d for x in cfg.destinations)]

        def on_progress(p: Progress) -> None:
            running.progress = p

        res = self.engine.run(
            job,
            dests,
            RunOptions(task.trigger, task.override_guard, task.only_dest_ids, task.force_rels),
            on_progress,
            running.cancel,
        )
        self._notify_run(job, res)
        return res

    def _notify_run(self, job: Job, res: RunResult) -> None:
        copied = sum(d.copied + d.versioned for d in res.dests)
        nbytes = sum(d.bytes for d in res.dests)
        if res.status == db.OK:
            if copied:
                self.notifier(f"Backup «{job.name}» completato", f"{copied} file copiati ({human_bytes(nbytes)})")
        elif res.status == db.PAUSED:
            self.notifier(f"Backup «{job.name}» IN PAUSA", "Rilevate modifiche sospette: controlla l'interfaccia")
        elif res.status == db.SKIPPED:
            self.notifier(f"Backup «{job.name}» rimandato", "Nessun disco di backup collegato")
        elif res.status in (db.WARNING, db.FAILED):
            self.notifier(
                f"Backup «{job.name}»: {'attenzione' if res.status == db.WARNING else 'non riuscito'}",
                res.message[:200] or "Vedi i dettagli nell'interfaccia",
            )

    def _do_verify(self, cfg: AppConfig, job: Job, task: Task, running: Running) -> verify_mod.VerifyReport | None:
        if task.dest_id is None:
            return None
        dest = cfg.destination(task.dest_id)
        running.label = f"Verifica su {dest.name}"
        vid = self.catalog.start_verify(job.id, dest.id, job.name, dest.name)
        status = destinations.check(dest)
        if not status.ready:
            self.catalog.finish_verify(vid, db.SKIPPED, 0, [], [], 0, status.message)
            return None
        try:
            cipher = destinations.load_cipher(dest)
            rep = verify_mod.verify(restore_mod.job_layout(dest, job), cipher, cancel=running.cancel)
        except Exception as e:
            self.catalog.finish_verify(vid, db.FAILED, 0, [], [], 0, str(e))
            return None
        st = db.OK if rep.healthy else db.FAILED
        msg = (
            f"{rep.ok}/{rep.checked} integri"
            + (f", {len(rep.corrupted)} CORROTTI" if rep.corrupted else "")
            + (f", {len(rep.missing)} mancanti (verranno ricopiati)" if rep.missing else "")
        )
        self.catalog.finish_verify(vid, st, rep.checked, rep.corrupted, rep.missing, rep.unverifiable, msg)
        self.audit.log("verify_finished", job=job.name, dest=dest.name, status=st, corrupted=len(rep.corrupted))
        if rep.corrupted:
            self.notifier(f"Verifica «{job.name}» su {dest.name}", f"{len(rep.corrupted)} file corrotti: li ricopio")
            # i file corrotti in current/ vengono riscritti dal PC (la copia corrotta finisce in versions/)
            current = {r for r in rep.corrupted if r.startswith("current/")}
            if current:
                self.enqueue(Task("backup", job.id, "verify", only_dest_ids={dest.id}, force_rels={dest.id: current}))
        return rep

    # ------------------------------------------------------------------ ripristino
    def restore(
        self, job_id: str, dest_id: str, area: str, rel: str, target_dir: str | None
    ) -> restore_mod.RestoreReport:
        cfg = self.config
        job, dest = cfg.job(job_id), cfg.destination(dest_id)
        status = destinations.check(dest)
        if not status.ready:
            raise FileNotFoundError(status.message)
        cipher = destinations.load_cipher(dest) if dest.encrypted else None
        rep = restore_mod.restore(
            restore_mod.job_layout(dest, job), job, area, rel, Path(target_dir) if target_dir else None, cipher
        )
        self.audit.log(
            "restore",
            actor="user",
            job=job.name,
            dest=dest.name,
            area=area,
            path=rel,
            target=target_dir or "originale",
            restored=len(rep.restored),
            errors=len(rep.errors),
        )
        return rep

    # ------------------------------------------------------------------ stato
    def snapshot(self) -> dict[str, object]:
        r = self.running
        out: dict[str, object] = {"running": None, "queued": [t.job_id for t in self.queued()]}
        if r:
            p = r.progress
            out["running"] = {
                "job_id": r.task.job_id,
                "job_name": r.job_name,
                "kind": r.task.kind,
                "label": r.label,
                "elapsed": int(time.time() - r.started),
                "phase": p.phase if p else "scan",
                "dest": p.dest_name if p else "",
                "files_done": p.files_done if p else 0,
                "files_total": p.files_total if p else 0,
                "bytes_done": p.bytes_done if p else 0,
                "bytes_total": p.bytes_total if p else 0,
                "current": p.current if p else "",
            }
        return out

    def last_run_time(self, job_id: str) -> datetime | None:
        return self.catalog.last_started_at(job_id)
