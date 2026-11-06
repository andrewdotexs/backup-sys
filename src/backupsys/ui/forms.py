"""Conversione tra form HTML e modelli di configurazione."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backupsys.config.models import Destination, Job, RetentionPolicy, Schedule


def lines(text: str) -> list[str]:
    return [x.strip() for x in text.replace("\r", "").split("\n") if x.strip()]


@dataclass
class ScheduleForm:
    kind: str = "manual"  # manual | daily | weekly | interval | cron
    time: str = "13:00"
    weekday: int = 1
    interval_hours: int = 4
    cron: str = ""
    on_connect: bool = False


def schedule_to_form(s: Schedule) -> ScheduleForm:
    f = ScheduleForm(on_connect=s.on_connect)
    if s.interval_minutes:
        f.kind, f.interval_hours = "interval", max(1, s.interval_minutes // 60)
    elif s.cron:
        p = s.cron.split()
        if len(p) == 5 and p[0].isdigit() and p[1].isdigit() and p[2] == "*" and p[3] == "*":
            f.time = f"{int(p[1]):02d}:{int(p[0]):02d}"
            if p[4] == "*":
                f.kind = "daily"
            elif p[4].isdigit():
                f.kind, f.weekday = "weekly", int(p[4]) % 7
            else:
                f.kind, f.cron = "cron", s.cron
        else:
            f.kind, f.cron = "cron", s.cron
    return f


def form_to_schedule(f: ScheduleForm) -> Schedule:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", f.time.strip() or "13:00")
    if f.kind in ("daily", "weekly") and not m:
        raise ValueError("Orario non valido (usa HH:MM)")
    hh, mm = (int(m.group(1)), int(m.group(2))) if m else (13, 0)
    if hh > 23 or mm > 59:
        raise ValueError("Orario non valido")
    if f.kind == "daily":
        return Schedule(cron=f"{mm} {hh} * * *", on_connect=f.on_connect)
    if f.kind == "weekly":
        return Schedule(cron=f"{mm} {hh} * * {f.weekday % 7}", on_connect=f.on_connect)
    if f.kind == "interval":
        return Schedule(interval_minutes=max(1, f.interval_hours) * 60, on_connect=f.on_connect)
    if f.kind == "cron":
        return Schedule(cron=f.cron, on_connect=f.on_connect)
    return Schedule(on_connect=f.on_connect)


def job_from_form(form: dict[str, object], existing: Job | None) -> Job:
    def g(k: str, d: str = "") -> str:
        v = form.get(k, d)
        return str(v) if v is not None else d

    sched = form_to_schedule(
        ScheduleForm(
            kind=g("schedule_kind", "manual"),
            time=g("schedule_time", "13:00"),
            weekday=int(g("schedule_weekday", "1") or 1),
            interval_hours=int(g("schedule_interval", "4") or 4),
            cron=g("schedule_cron"),
            on_connect=bool(form.get("schedule_on_connect")),
        )
    )
    dests = form.get("destinations") or []
    data = {
        "name": g("name"),
        "sources": lines(g("sources")),
        "excludes": lines(g("excludes")),
        "destinations": list(dests) if isinstance(dests, list) else [str(dests)],
        "schedule": sched,
        "enabled": bool(form.get("enabled")),
        "deep_compare": bool(form.get("deep_compare")),
        "guard_threshold_percent": float(g("guard_threshold", "30") or 30),
        "guard_min_files": int(g("guard_min_files", "50") or 50),
        "verify_every_days": int(g("verify_every_days")) if g("verify_every_days").strip() else None,
    }
    if existing:
        return existing.model_copy(update=data)
    return Job(**data)


def dest_from_form(form: dict[str, object], existing: Destination | None) -> Destination:
    def g(k: str, d: str = "") -> str:
        v = form.get(k, d)
        return str(v) if v is not None else d

    path = g("path").strip()
    kind = "network" if path.startswith("\\\\") or form.get("kind") == "network" else "local"
    data = {
        "name": g("name").strip() or path,
        "path": path,
        "kind": kind,
        "network_username": g("network_username").strip() or None,
        "retention": RetentionPolicy(
            max_versions_percent=float(g("max_versions_percent", "30") or 30),
            min_free_percent=float(g("min_free_percent", "10") or 10),
            min_age_days=int(g("min_age_days", "30") or 30),
        ),
    }
    if existing:
        # volume_id resta invariato: se il nuovo percorso punta a un altro disco, il backup lo rifiuterà
        return existing.model_copy(update=data)
    return Destination(**data)
