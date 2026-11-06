"""Orchestrazione di un'esecuzione di backup su una o più destinazioni.

Per ogni destinazione: verifica identità → piano → guard anti-ransomware → rotazione versioni
e controllo spazio → copia tramite SafeWriter → manifest → registrazione nel catalogo.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backupsys.catalog import db
from backupsys.catalog.db import Catalog
from backupsys.config.models import Destination, Job
from backupsys.core import retention
from backupsys.core.fsutil import disk_usage, human_bytes, long_path
from backupsys.core.layout import DestLayout, run_timestamp
from backupsys.core.locks import DestinationLock, LockBusyError
from backupsys.core.manifest import ManifestWriter
from backupsys.core.planner import Action, PlannedItem, build_plan
from backupsys.core.renames import RenameMatch, find_renames
from backupsys.core.safe_writer import SafeWriter, SourceChangedError
from backupsys.core.scanner import ScanResult, scan
from backupsys.destinations import base as destinations
from backupsys.security import crypto, guard
from backupsys.security.audit import AuditLog

log = logging.getLogger(__name__)


@dataclass
class Progress:
    job_id: str
    job_name: str
    phase: str = "scan"  # scan | plan | copy | done
    dest_name: str = ""
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""


@dataclass
class RunOptions:
    trigger: str = "manual"  # manual | schedule | connect | confirm
    override_guard: bool = False
    only_dest_ids: set[str] | None = None
    #: dest_id -> percorsi (relativi al job) da riscrivere comunque (file trovati corrotti dalla verifica)
    force_rels: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class DestResult:
    dest_id: str
    name: str
    status: str
    copied: int = 0
    versioned: int = 0
    skipped: int = 0
    linked: int = 0  # rinominati/spostati sul PC: collegati al file già presente, senza ricopiarli
    bytes: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    pruned: list[tuple[str, int]] = field(default_factory=list)
    message: str = ""


@dataclass
class RunResult:
    run_id: int
    run_ts: str
    status: str
    dests: list[DestResult] = field(default_factory=list)
    scan_errors: list[tuple[str, str]] = field(default_factory=list)
    files_scanned: int = 0
    message: str = ""


ProgressCb = Callable[[Progress], None]
BindCb = Callable[[str, str], None]


def _overall(statuses: list[str]) -> str:
    if not statuses:
        return db.FAILED
    if db.PAUSED in statuses:
        return db.PAUSED
    if db.CANCELLED in statuses:
        return db.CANCELLED
    if all(s == db.OK for s in statuses):
        return db.OK
    if all(s == db.SKIPPED for s in statuses):
        return db.SKIPPED
    if all(s == db.FAILED for s in statuses):
        return db.FAILED
    return db.WARNING


class BackupEngine:
    def __init__(
        self,
        catalog: Catalog,
        audit: AuditLog,
        on_bind: BindCb | None = None,
        on_relocate: BindCb | None = None,
    ) -> None:
        self.catalog = catalog
        self.audit = audit
        self.on_bind = on_bind
        self.on_relocate = on_relocate  # (dest_id, nuovo percorso): il disco ha cambiato lettera

    def run(
        self,
        job: Job,
        dests: list[Destination],
        options: RunOptions | None = None,
        progress: ProgressCb | None = None,
        cancel: threading.Event | None = None,
    ) -> RunResult:
        options = options or RunOptions()
        cancel = cancel or threading.Event()
        prog = Progress(job.id, job.name)
        emit = progress or (lambda p: None)
        run_ts = run_timestamp()
        run_id = self.catalog.start_run(job.id, job.name, run_ts, options.trigger)
        self.audit.log("run_started", job=job.name, run_id=run_id, trigger=options.trigger)
        result = RunResult(run_id, run_ts, db.RUNNING)
        try:
            emit(prog)
            scanned: ScanResult = scan(job.sources, job.excludes, job.follow_symlinks)
            result.scan_errors = scanned.errors
            result.files_scanned = len(scanned.files)
            self.catalog.record_errors(run_id, None, scanned.errors)
            if scanned.unreadable_sources and len(scanned.unreadable_sources) >= len(job.sources):
                # niente da salvare: non ha senso dichiarare "completato" su nessun disco
                result.status = db.FAILED
                result.message = (
                    "Nessuna cartella sorgente leggibile: " + ", ".join(scanned.unreadable_sources) + ". "
                    "Controlla che i percorsi esistano e siano accessibili (vedi errori)."
                )
                return result
            selected = [d for d in dests if options.only_dest_ids is None or d.id in options.only_dest_ids]
            for dest in selected:
                if cancel.is_set():
                    result.dests.append(DestResult(dest.id, dest.name, db.CANCELLED, message="Annullato"))
                    continue
                prog.dest_name = dest.name
                dr = self._run_dest(job, dest, scanned, options, prog, emit, cancel, run_ts)
                result.dests.append(dr)
                self.catalog.record_dest(
                    run_id,
                    dest.id,
                    dest.name,
                    status=dr.status,
                    copied=dr.copied,
                    versioned=dr.versioned,
                    linked=dr.linked,
                    skipped=dr.skipped,
                    bytes=dr.bytes,
                    errors=len(dr.errors),
                    pruned_versions=len(dr.pruned),
                    pruned_bytes=sum(b for _, b in dr.pruned),
                    message=dr.message,
                )
                self.catalog.record_errors(run_id, dest.id, dr.errors)
                if dr.status in (db.OK, db.WARNING):
                    self.catalog.mark_success(job.id, dest.id)
                if dr.status != db.SKIPPED and DestLayout(Path(dest.path)).root.is_dir():
                    try:  # occupazione aggiornata per la panoramica (una sola lettura del disco)
                        backup_b, versions_b = retention.measure(DestLayout(Path(dest.path)))
                        self.catalog.record_usage(dest.id, backup_b, versions_b)
                    except OSError:
                        pass
            statuses = [d.status for d in result.dests]
            result.status = _overall(statuses)
            if result.status == db.OK and scanned.errors:
                result.status = db.WARNING
            grouped: dict[str, list[str]] = {}
            for d in result.dests:
                if d.message:
                    grouped.setdefault(d.message, []).append(d.name)
            result.message = "; ".join(f"{', '.join(names)}: {msg}" for msg, names in grouped.items())
            if scanned.unreadable_sources:
                result.message = (
                    "Cartelle sorgente non leggibili: " + ", ".join(scanned.unreadable_sources) + ". " + result.message
                ).strip()
            if not selected:
                result.message = "Nessuna destinazione configurata"
        except Exception as e:  # errore imprevisto: registrato, mai propagato al daemon
            log.exception("Backup %s fallito", job.name)
            result.status = db.FAILED
            result.message = f"Errore imprevisto: {e}"
        finally:
            self.catalog.finish_run(run_id, result.status, result.files_scanned, 0, result.message[:2000])
            self.audit.log("run_finished", job=job.name, run_id=run_id, status=result.status)
            prog.phase = "done"
            emit(prog)
        return result

    @staticmethod
    def _try_link(writer: SafeWriter, item: PlannedItem, match: RenameMatch) -> bool:
        """Collega un file rinominato; False se il file system non lo permette (si copierà normalmente)."""
        try:
            writer.link(item, match.existing, match.sha256)
            return True
        except FileExistsError:
            raise
        except OSError:
            return False

    # ------------------------------------------------------------------ singola destinazione
    def _run_dest(
        self,
        job: Job,
        dest: Destination,
        scanned: ScanResult,
        options: RunOptions,
        prog: Progress,
        emit: ProgressCb,
        cancel: threading.Event,
        run_ts: str,
    ) -> DestResult:
        dr = DestResult(dest.id, dest.name, db.RUNNING)
        status = destinations.check(dest)
        if status.relocated_to:
            self.audit.log("dest_relocated", dest=dest.name, old=dest.path, new=status.relocated_to)
            dest = dest.model_copy(update={"path": status.relocated_to})
            if self.on_relocate:
                self.on_relocate(dest.id, status.relocated_to)
        if not status.available:
            dr.status, dr.message = db.SKIPPED, status.message
            return dr
        if not status.identity_ok:
            dr.status, dr.message = db.FAILED, status.message
            self.audit.log("dest_identity_mismatch", dest=dest.name, message=status.message)
            return dr
        if dest.volume_id is None:
            try:
                vid = destinations.bind(dest)
            except (destinations.DiskNotEmptyError, OSError) as e:
                dr.status, dr.message = db.FAILED, str(e)
                return dr
            dest = dest.model_copy(update={"volume_id": vid})
            if self.on_bind:
                self.on_bind(dest.id, vid)
            self.audit.log("dest_bound", dest=dest.name, volume_id=vid)
        try:
            cipher = destinations.load_cipher(dest)
        except PermissionError as e:
            dr.status, dr.message = db.FAILED, str(e)
            return dr
        try:
            with DestinationLock(dest.id):
                return self._copy(job, dest, scanned, options, prog, emit, cancel, run_ts, cipher, dr)
        except LockBusyError as e:
            dr.status, dr.message = db.FAILED, str(e)
            return dr

    def _copy(
        self,
        job: Job,
        dest: Destination,
        scanned: ScanResult,
        options: RunOptions,
        prog: Progress,
        emit: ProgressCb,
        cancel: threading.Event,
        run_ts: str,
        cipher: crypto.StreamCipher | None,
        dr: DestResult,
    ) -> DestResult:
        dlayout = DestLayout(Path(dest.path))
        jlayout = dlayout.job(job.slug)
        os.makedirs(long_path(jlayout.current), exist_ok=True)
        ts = jlayout.unique_run_ts(run_ts)

        prog.phase, prog.files_done, prog.bytes_done = "plan", 0, 0
        emit(prog)
        plan = build_plan(
            scanned.files, jlayout, dest.encrypted, cipher, job.deep_compare, options.force_rels.get(dest.id)
        )
        dr.errors.extend(plan.errors)
        dr.skipped = plan.count(Action.SKIP)

        # 1) guard anti-ransomware, PRIMA di qualunque scrittura o rotazione
        verdict = guard.evaluate(plan, job.guard_threshold_percent, job.guard_min_files)
        if verdict.suspicious and not options.override_guard:
            dr.status = db.PAUSED
            dr.message = f"In pausa per sicurezza: {verdict.reason}"
            self.audit.log("guard_paused", job=job.name, dest=dest.name, reason=verdict.reason)
            return dr
        if verdict.suspicious:
            self.audit.log("guard_overridden", actor="user", job=job.name, dest=dest.name, reason=verdict.reason)

        todo = plan.to_transfer
        # file rinominati/spostati sul PC: verranno collegati (hard link) al file già sul backup
        prog.phase = "plan"
        renames = find_renames(plan, jlayout, dest.encrypted, cipher)
        need = sum(
            crypto.encrypted_size(i.src.size) if dest.encrypted else i.src.size
            for i in todo
            if i.dest_path not in renames
        )

        # 2) rotazione versioni secondo la policy del disco, tenendo conto di ciò che va scritto
        report = retention.enforce(
            dlayout,
            dest.retention,
            reserve_bytes=need,
            protect={ts},
            log=lambda folder, size: self.audit.log(
                "version_pruned", job=job.name, dest=dest.name, folder=folder, bytes=size
            ),
        )
        dr.pruned = report.deleted
        quota = int(dest.retention.max_backup_gb * 2**30) if dest.retention.max_backup_gb else None
        if quota and report.backup_bytes_after + need > quota:
            dr.status = db.FAILED
            dr.message = (
                f"Quota del disco superata: backup-sys occupa {human_bytes(report.backup_bytes_after)}, "
                f"servono altri {human_bytes(need)}, quota {human_bytes(quota)}. " + report.message
            )
            self.audit.log("dest_quota_exceeded", job=job.name, dest=dest.name, need=need, quota=quota)
            return dr
        total, _used, free = disk_usage(dest.path)
        if need + retention.space_margin(total) > free:
            dr.status = db.FAILED
            dr.message = f"Spazio insufficiente: servono {human_bytes(need)}, liberi {human_bytes(free)}. " + (
                report.message
            )
            self.audit.log("dest_full", job=job.name, dest=dest.name, need=need, free=free)
            return dr

        # 3) copia
        prog.phase, prog.files_total, prog.bytes_total = "copy", len(todo), sum(i.src.size for i in todo)
        emit(prog)
        manifest = ManifestWriter(jlayout.manifests, ts)
        writer = SafeWriter(jlayout, ts, manifest, cipher)
        try:
            for item in todo:
                if cancel.is_set():
                    dr.status, dr.message = db.CANCELLED, "Annullato dall'utente"
                    break
                prog.current = item.src.backup_rel
                try:
                    match = renames.get(item.dest_path)
                    if match is not None and self._try_link(writer, item, match):
                        dr.linked += 1
                    else:
                        writer.store(item)
                        if item.action is Action.COPY:
                            dr.copied += 1
                        else:
                            dr.versioned += 1
                        dr.bytes += item.src.size
                except SourceChangedError:
                    dr.errors.append((item.src.abs_path, "Modificato durante la copia: verrà ripreso"))
                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        dr.errors.append((item.src.abs_path, "Disco pieno"))
                        dr.status, dr.message = db.FAILED, "Disco di backup pieno durante la copia"
                        break
                    dr.errors.append((item.src.abs_path, e.strerror or str(e)))
                    if not os.path.isdir(dest.path):  # disco scollegato a metà
                        dr.status, dr.message = db.FAILED, "Destinazione scollegata durante la copia"
                        break
                prog.files_done += 1
                prog.bytes_done += item.src.size
                if prog.files_done % 25 == 0:
                    emit(prog)
        finally:
            with contextlib.suppress(OSError):
                manifest.close()
        if dr.status == db.RUNNING:
            dr.status = db.WARNING if dr.errors else db.OK
            if not report.within_limits:
                dr.message = report.message
        if dr.linked and not dr.message:
            dr.message = f"{dr.linked} file rinominati o spostati riconosciuti: collegati senza ricopiarli"
        if dr.pruned and not dr.message:
            dr.message = f"Ruotate {len(dr.pruned)} cartelle di versioni ({human_bytes(report.freed_bytes)})"
        return dr
