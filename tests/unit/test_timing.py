from datetime import datetime, timedelta

from backupsys.config.models import Schedule
from backupsys.scheduler.timing import describe, dest_is_behind, next_run


def test_daily_cron_next_and_catch_up() -> None:
    s = Schedule(cron="0 13 * * *")
    anchor = datetime(2026, 11, 6, 10, 0)
    assert next_run(s, None, anchor) == datetime(2026, 11, 6, 13, 0)
    # ultimo avvio ieri alle 13: oggi alle 13 è dovuto; se il PC era spento ed è ora 18, è già nel passato
    assert next_run(s, datetime(2026, 11, 5, 13, 0, 5), anchor) == datetime(2026, 11, 6, 13, 0)


def test_interval() -> None:
    s = Schedule(interval_minutes=240)
    anchor = datetime(2026, 11, 6, 10, 0)
    assert next_run(s, None, anchor) == anchor  # mai eseguito: subito
    assert next_run(s, anchor, anchor) == anchor + timedelta(hours=4)


def test_manual_never_scheduled() -> None:
    assert next_run(Schedule(), None, datetime.now()) is None
    assert next_run(Schedule(on_connect=True), None, datetime.now()) is None


def test_dest_behind() -> None:
    s = Schedule(cron="0 13 * * *")
    now = datetime(2026, 11, 6, 15, 0)
    assert dest_is_behind(s, None, now)
    assert dest_is_behind(s, datetime(2026, 11, 5, 13, 1), now)
    assert not dest_is_behind(s, datetime(2026, 11, 6, 13, 1), now)
    assert not dest_is_behind(Schedule(on_connect=True), None, now)


def test_describe() -> None:
    assert describe(Schedule(cron="30 13 * * *")) == "ogni giorno alle 13:30"
    assert describe(Schedule(cron="0 9 * * 1")) == "ogni lunedì alle 09:00"
    assert describe(Schedule(interval_minutes=120, on_connect=True)) == "ogni 2 ore, al collegamento del disco"
    assert describe(Schedule()) == "solo manuale"
