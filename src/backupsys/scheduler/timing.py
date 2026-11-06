"""Calcolo delle esecuzioni dovute (funzioni pure, facili da testare)."""

from __future__ import annotations

from datetime import datetime, timedelta

from croniter import croniter

from backupsys.config.models import Schedule


def next_run(schedule: Schedule, last_started: datetime | None, anchor: datetime) -> datetime | None:
    """Prossima esecuzione programmata.

    ``last_started``: ultimo avvio del job (None se mai eseguito); ``anchor``: da quando il job è noto
    allo scheduler. Se il PC era spento all'orario previsto, il risultato è nel passato e il job parte subito
    (recupero delle esecuzioni perse).
    """
    base = last_started or anchor
    if schedule.cron:
        nxt: datetime = croniter(schedule.cron, base).get_next(datetime)
        return nxt
    if schedule.interval_minutes:
        if last_started is None:
            return anchor
        return last_started + timedelta(minutes=schedule.interval_minutes)
    return None


def last_scheduled_fire(schedule: Schedule, now: datetime) -> datetime | None:
    """Ultimo orario programmato <= now (per capire se un disco è "rimasto indietro")."""
    if schedule.cron:
        prev: datetime = croniter(schedule.cron, now).get_prev(datetime)
        return prev
    if schedule.interval_minutes:
        return now - timedelta(minutes=schedule.interval_minutes)
    return None


def dest_is_behind(schedule: Schedule, last_success: datetime | None, now: datetime) -> bool:
    if schedule.is_manual or (not schedule.cron and not schedule.interval_minutes):
        return False
    if last_success is None:
        return True
    fire = last_scheduled_fire(schedule, now)
    return fire is not None and last_success < fire


def describe(schedule: Schedule) -> str:
    parts = []
    if schedule.cron:
        parts.append(_describe_cron(schedule.cron))
    if schedule.interval_minutes:
        m = schedule.interval_minutes
        parts.append(f"ogni {m // 60} ore" if m % 60 == 0 and m >= 60 else f"ogni {m} minuti")
    if schedule.on_connect:
        parts.append("al collegamento del disco")
    return ", ".join(parts) or "solo manuale"


GIORNI = ["domenica", "lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato"]


def _describe_cron(expr: str) -> str:
    f = expr.split()
    if len(f) == 5 and f[0].isdigit() and f[1].isdigit() and f[2] == "*" and f[3] == "*":
        hhmm = f"{int(f[1]):02d}:{int(f[0]):02d}"
        if f[4] == "*":
            return f"ogni giorno alle {hhmm}"
        if f[4].isdigit():
            return f"ogni {GIORNI[int(f[4]) % 7]} alle {hhmm}"
        if f[4] == "1-5":
            return f"dal lunedì al venerdì alle {hhmm}"
    return f"cron «{expr}»"
