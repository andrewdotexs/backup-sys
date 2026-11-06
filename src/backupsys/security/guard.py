"""Guard anti-ransomware.

Un ransomware cifra/rinomina molti file in poco tempo. Se in una singola esecuzione la quota di file
già presenti sul backup che risultano modificati supera la soglia, o compaiono molti file con estensioni
tipiche dei ransomware, l'esecuzione viene messa in pausa e serve una conferma esplicita dell'utente.
Il controllo avviene PRIMA di qualunque scrittura o rotazione delle versioni.
"""

from __future__ import annotations

from dataclasses import dataclass

from backupsys.core.planner import Action, Plan

SUSPICIOUS_EXTENSIONS = frozenset(
    {
        ".locked",
        ".encrypted",
        ".enc",
        ".crypt",
        ".crypted",
        ".crypto",
        ".cry",
        ".wncry",
        ".wnry",
        ".locky",
        ".zepto",
        ".cerber",
        ".cerber3",
        ".ryk",
        ".ryuk",
        ".conti",
        ".lockbit",
        ".abcd",
        ".djvu",
        ".stop",
        ".pay",
        ".ransom",
        ".r5a",
        ".zzzzz",
        ".aaa",
        ".xyz",
        ".micro",
        ".vvv",
    }
)
SUSPICIOUS_MIN_COUNT = 10


@dataclass(frozen=True)
class GuardVerdict:
    suspicious: bool
    reason: str = ""
    changed: int = 0
    existing: int = 0
    suspicious_ext: int = 0


def evaluate(plan: Plan, threshold_percent: float, min_files: int) -> GuardVerdict:
    existing = plan.existing_count
    changed = plan.count(Action.NEW_VERSION)
    sus = sum(
        1
        for i in plan.to_transfer
        if "." in i.src.rel.rsplit("/", 1)[-1] and "." + i.src.rel.rsplit(".", 1)[-1].lower() in SUSPICIOUS_EXTENSIONS
    )
    if sus >= SUSPICIOUS_MIN_COUNT:
        return GuardVerdict(
            True,
            f"{sus} file con estensioni tipiche dei ransomware (es. .locked, .encrypted)",
            changed,
            existing,
            sus,
        )
    if existing >= min_files:
        pct = changed * 100 / existing
        if pct >= threshold_percent:
            return GuardVerdict(
                True,
                f"{changed} file su {existing} ({pct:.0f}%) risultano modificati in un'unica esecuzione "
                f"(soglia {threshold_percent:.0f}%)",
                changed,
                existing,
                sus,
            )
    return GuardVerdict(False, "", changed, existing, sus)
