from backupsys.core.planner import Action, Plan, PlannedItem
from backupsys.core.scanner import SourceFile
from backupsys.security.guard import evaluate


def _plan(changed: int, same: int, new_ext: str | None = None, new: int = 0) -> Plan:
    p = Plan()
    for i in range(changed):
        p.items.append(PlannedItem(SourceFile("K", f"c{i}.docx", "", 1, 1), Action.NEW_VERSION, ""))
    for i in range(same):
        p.items.append(PlannedItem(SourceFile("K", f"s{i}.docx", "", 1, 1), Action.SKIP, ""))
    for i in range(new):
        p.items.append(PlannedItem(SourceFile("K", f"n{i}.docx{new_ext or ''}", "", 1, 1), Action.COPY, ""))
    return p


def test_below_threshold_ok() -> None:
    assert not evaluate(_plan(10, 90), 30, 50).suspicious


def test_above_threshold_paused() -> None:
    v = evaluate(_plan(40, 60), 30, 50)
    assert v.suspicious and "40 file su 100" in v.reason


def test_small_backup_not_guarded() -> None:
    assert not evaluate(_plan(10, 0), 30, 50).suspicious


def test_ransomware_extensions() -> None:
    assert evaluate(_plan(0, 0, ".locked", new=12), 30, 50).suspicious
