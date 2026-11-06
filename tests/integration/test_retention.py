from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backupsys.config.models import RetentionPolicy
from backupsys.core import retention
from backupsys.core.layout import TS_FORMAT, DestLayout


def _make(disk: Path, job: str, ts: datetime, size: int) -> Path:
    v = disk / "backup-sys" / job / "versions" / ts.strftime(TS_FORMAT) / "C" / "f.bin"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"x" * size)
    (disk / "backup-sys" / job / "current" / "C").mkdir(parents=True, exist_ok=True)
    (disk / "backup-sys" / job / "current" / "C" / "keep.txt").write_text("mai cancellare")
    return v.parent.parent


def _usage(total: int, disk: Path, free_bonus: int = 2 * retention.SPACE_MARGIN):  # type: ignore[no-untyped-def]
    """Disco finto: capacità ``total`` per le percentuali; ``free_bonus`` evita di far scattare la regola
    "lo spazio libero non basta" nei test che non la riguardano."""

    def f(_p: Path) -> tuple[int, int, int]:
        used = retention.tree_size(disk)
        return total, used, total - used + free_bonus

    return f


def test_prunes_oldest_until_within_limits(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0)
    folders = [_make(tmp_path, "job-a", now - timedelta(days=100 - i * 10), 1000) for i in range(5)]
    # versioni = 5000 byte su un disco "da 10000": limite 30% = 3000 -> devono sparire le 2 più vecchie
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=30, min_age_days=0),
        now=now,
        usage=_usage(10_000, tmp_path),
    )
    assert [Path(p) for p, _ in rep.deleted] == folders[:2]
    assert all(f.exists() for f in folders[2:])
    assert rep.within_limits
    assert (tmp_path / "backup-sys" / "job-a" / "current" / "C" / "keep.txt").exists()


def test_respects_min_age(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0)
    old = _make(tmp_path, "job-a", now - timedelta(days=60), 3000)
    young = _make(tmp_path, "job-a", now - timedelta(days=5), 3000)
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=10, min_age_days=30),
        now=now,
        usage=_usage(10_000, tmp_path),
    )
    assert not old.exists() and young.exists()
    assert not rep.within_limits and "troppo recenti" in rep.message


def test_prunes_across_jobs_by_age(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6)
    a = _make(tmp_path, "job-a", now - timedelta(days=50), 2000)
    b = _make(tmp_path, "job-b", now - timedelta(days=90), 2000)
    retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=30, min_age_days=0),
        now=now,
        usage=_usage(10_000, tmp_path),
    )
    assert not b.exists() and a.exists()


def test_rotates_when_free_space_not_enough_for_copy(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6)
    f = _make(tmp_path, "job-a", now - timedelta(days=50), 1000)
    total = 10_000  # margine = 2% = 200 byte
    # libero = total - usato (~8986); servono 8900 byte + margine: senza ruotare non ci stanno
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=90, min_age_days=0),
        reserve_bytes=8_900,
        now=now,
        usage=_usage(total, tmp_path, free_bonus=0),
    )
    assert not f.exists() and rep.freed_bytes == 1000 and rep.within_limits


def test_free_space_is_not_kept_free_for_its_own_sake(tmp_path: Path) -> None:
    """Opzione (b): niente più "mantieni il 10% libero"; si ruota solo se la copia non ci sta."""
    now = datetime(2026, 11, 6)
    f = _make(tmp_path, "job-a", now - timedelta(days=50), 1000)
    total = 10_000  # disco con versioni al 10%, la copia (100 byte) ci sta abbondantemente
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=90, min_age_days=0),
        reserve_bytes=100,
        now=now,
        usage=_usage(total, tmp_path, free_bonus=0),
    )
    assert f.exists() and rep.deleted == []


def test_quota_limits_backup_size(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6)
    old = _make(tmp_path, "job-a", now - timedelta(days=90), 600_000)
    new = _make(tmp_path, "job-a", now - timedelta(days=40), 600_000)
    gb = 2**30
    # quota di ~1.5 MB: le due versioni (1.2 MB) + 400 KB da scrivere la superano -> ruota la più vecchia
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_backup_gb=1_500_000 / gb, max_versions_percent=95, min_age_days=0),
        reserve_bytes=400_000,
        now=now,
        usage=_usage(10**12, tmp_path),  # disco enorme: conta solo la quota
    )
    assert not old.exists() and new.exists() and rep.within_limits


def test_versions_percent_relative_to_quota(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6)
    old = _make(tmp_path, "job-a", now - timedelta(days=90), 400_000)
    rep = retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_backup_gb=1_000_000 / 2**30, max_versions_percent=30, min_age_days=0),
        now=now,
        usage=_usage(10**12, tmp_path),
    )
    assert not old.exists() and rep.within_limits


def test_measure(tmp_path: Path) -> None:
    _make(tmp_path, "job-a", datetime(2026, 1, 1), 1000)
    total, versions = retention.measure(DestLayout(tmp_path))
    assert versions == 1000 and total == 1000 + len("mai cancellare")


def test_nothing_to_do_within_limits(tmp_path: Path) -> None:
    f = _make(tmp_path, "job-a", datetime(2020, 1, 1), 100)
    rep = retention.enforce(DestLayout(tmp_path), RetentionPolicy(), usage=_usage(10**9, tmp_path))
    assert f.exists() and rep.deleted == []


def test_safety_check_rejects_non_version_paths(tmp_path: Path) -> None:
    root = tmp_path / "backup-sys"
    (root / "job" / "current" / "2026-01-01T00-00-00").mkdir(parents=True)
    (root / "job" / "versions" / "nonunadata").mkdir(parents=True)
    with pytest.raises(retention.RetentionSafetyError):
        retention._assert_deletable(root / "job" / "current" / "2026-01-01T00-00-00", root)
    with pytest.raises(retention.RetentionSafetyError):
        retention._assert_deletable(root / "job" / "versions" / "nonunadata", root)
    with pytest.raises(retention.RetentionSafetyError):
        retention._assert_deletable(tmp_path / "altro" / "versions" / "2026-01-01T00-00-00", root)


@pytest.mark.skipif(os.name == "nt", reason="i symlink richiedono privilegi su Windows")
def test_symlinked_version_folder_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "preziosi"
    target.mkdir()
    (target / "importante.txt").write_text("!")
    v = tmp_path / "backup-sys" / "job" / "versions"
    v.mkdir(parents=True)
    (tmp_path / "backup-sys" / "job" / "current").mkdir()
    (v / "2020-01-01T00-00-00").symlink_to(target, target_is_directory=True)
    retention.enforce(
        DestLayout(tmp_path),
        RetentionPolicy(max_versions_percent=1, min_age_days=0),
        usage=lambda p: (100, 100, 0),
    )
    assert (target / "importante.txt").exists()


def test_space_margin_scales_with_disk() -> None:
    assert retention.space_margin(50 * 2**20) == 1 * 2**20  # chiavetta da 50 MB: 1 MB
    assert retention.space_margin(2 * 2**40) == retention.SPACE_MARGIN  # disco da 2 TB: 64 MB
