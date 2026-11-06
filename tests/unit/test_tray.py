from backupsys.tray import TOOLTIP_MAX, progress_percent, render_icon, status_text


def _snap(**run: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "backup",
        "job_name": "Documenti",
        "dest": "USB-128GB",
        "phase": "copy",
        "files_done": 0,
        "files_total": 0,
        "bytes_done": 0,
        "bytes_total": 0,
    }
    base.update(run)
    return {"running": base, "queued": []}


def test_idle() -> None:
    s = {"running": None, "queued": []}
    assert progress_percent(s) is None
    assert status_text(s) == "backup-sys — nessun backup in corso"
    assert status_text({"running": None, "queued": ["a", "b"]}) == "backup-sys — 2 backup in coda"


def test_copy_percent_by_bytes() -> None:
    s = _snap(bytes_done=42, bytes_total=100, files_done=1, files_total=10)
    assert progress_percent(s) == 42
    assert status_text(s) == "«Documenti» → USB-128GB: 42%"


def test_copy_percent_by_files_when_no_bytes() -> None:
    assert progress_percent(_snap(files_done=3, files_total=4)) == 75
    assert progress_percent(_snap(bytes_done=200, bytes_total=100)) == 100  # mai oltre 100


def test_scan_and_verify_phases() -> None:
    assert progress_percent(_snap(phase="scan")) is None
    assert status_text(_snap(phase="scan")) == "«Documenti» → USB-128GB: analisi dei file…"
    assert "verifica" in status_text({"running": {"kind": "verify", "label": "Verifica su USB"}, "queued": []}).lower()


def test_tooltip_truncated() -> None:
    assert len(status_text(_snap(job_name="x" * 300, bytes_done=1, bytes_total=2))) <= TOOLTIP_MAX


def test_render_icon_variants() -> None:
    idle, busy, half, full = render_icon(), render_icon(busy=True), render_icon(50), render_icon(100)
    for img in (idle, busy, half, full):
        assert img.size == (64, 64)
    assert idle.tobytes() != busy.tobytes() != half.tobytes() != full.tobytes()
    assert render_icon(10).tobytes() != render_icon(90).tobytes()
