"""Loop di scheduling: orari/intervalli, recupero esecuzioni perse, collegamento dischi, verifiche periodiche."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from backupsys.destinations import base as destinations
from backupsys.scheduler.timing import dest_is_behind, next_run
from backupsys.service import BackupService

log = logging.getLogger(__name__)

TICK_SECONDS = 15


class Scheduler:
    def __init__(self, service: BackupService, tick: float = TICK_SECONDS) -> None:
        self.service = service
        self.tick = tick
        self._anchors: dict[str, datetime] = {}
        self._dest_ready: dict[str, bool] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once(datetime.now())
            except Exception:
                log.exception("Errore nello scheduler")
            self._stop.wait(self.tick)

    def next_run_for(self, job_id: str, now: datetime | None = None) -> datetime | None:
        cfg = self.service.config
        job = cfg.job(job_id)
        if not job.enabled:
            return None
        anchor = self._anchors.setdefault(job.id, now or datetime.now())
        return next_run(job.schedule, self.service.last_run_time(job.id), anchor)

    def run_once(self, now: datetime) -> None:
        cfg = self.service.config
        svc = self.service
        # 1) esecuzioni programmate (comprese quelle perse mentre il PC era spento)
        for job in cfg.jobs:
            if not job.enabled:
                continue
            anchor = self._anchors.setdefault(job.id, now)
            due = next_run(job.schedule, svc.last_run_time(job.id), anchor)
            if due is not None and due <= now and not svc.is_busy(job.id):
                svc.enqueue_backup(job.id, trigger="schedule")

        # 2) dischi collegati / scollegati
        for dest in cfg.destinations:
            try:
                ready = destinations.check(dest).ready
            except Exception:
                ready = False
            before = self._dest_ready.get(dest.id)
            self._dest_ready[dest.id] = ready
            if not ready or before is True:
                continue
            just_connected = before is False
            for job in cfg.jobs:
                if not job.enabled or dest.id not in job.destinations or svc.is_busy(job.id):
                    continue
                behind = dest_is_behind(job.schedule, svc.catalog.last_success(job.id, dest.id), now)
                if behind or (just_connected and job.schedule.on_connect):
                    svc.enqueue_backup(job.id, trigger="connect", only_dest_ids={dest.id})

        # 3) verifiche di integrità periodiche
        for job in cfg.jobs:
            if not job.enabled or not job.verify_every_days:
                continue
            for dest_id in job.destinations:
                if not self._dest_ready.get(dest_id):
                    continue
                last_ok = svc.catalog.last_success(job.id, dest_id)
                if last_ok is None:
                    continue  # nessun backup ancora: niente da verificare
                # prima verifica N giorni dopo il primo backup, poi ogni N giorni
                base = svc.catalog.last_verify_at(job.id, dest_id) or svc.catalog.first_run_at(job.id) or now
                if now - base > timedelta(days=job.verify_every_days) and not svc.is_busy(job.id):
                    svc.enqueue_verify(job.id, dest_id, trigger="schedule")
