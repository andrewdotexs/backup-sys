import os
from pathlib import Path

import pytest

from backupsys.core.fsutil import source_key
from backupsys.core.scanner import Excluder, scan


def test_source_key_windows_and_unc() -> None:
    assert source_key("C:\\Users\\andrea\\Documenti") == "C/Users/andrea/Documenti"
    assert source_key("d:/Foto") == "D/Foto"
    assert source_key("\\\\NAS\\share\\foto") == "UNC/NAS/share/foto"


def test_excluder_patterns() -> None:
    ex = Excluder(["*.tmp", "**/node_modules/**", "build/", "~$*"])
    assert ex.file_excluded("a/b/x.tmp")
    assert ex.file_excluded("x.tmp")
    assert not ex.file_excluded("x.tmpl")
    assert ex.dir_excluded("progetto/node_modules")
    assert ex.dir_excluded("node_modules")
    assert ex.file_excluded("doc/~$lettera.docx")
    assert not ex.file_excluded("doc/lettera.docx")


def test_scan_excludes_and_skips_backup_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "a" / "node_modules" / "pkg").mkdir(parents=True)
    (src / "a" / "node_modules" / "pkg" / "i.js").write_text("x")
    (src / "a" / "f.txt").write_text("hello")
    (src / "a" / "f.tmp").write_text("t")
    (src / "backup-sys").mkdir()
    (src / "backup-sys" / ".bsys-volume.json").write_text("{}")
    (src / "backup-sys" / "x.txt").write_text("no")
    res = scan([str(src)], ["*.tmp", "**/node_modules/**"])
    rels = [f.rel for f in res.files]
    assert rels == ["a/f.txt"]
    assert res.files[0].size == 5
    assert res.excluded == 2


def test_scan_missing_source_is_error(tmp_path: Path) -> None:
    res = scan([str(tmp_path / "manca")], [])
    assert res.files == [] and len(res.errors) == 1


@pytest.mark.skipif(os.name == "nt", reason="i symlink richiedono privilegi su Windows")
def test_scan_skips_symlinks(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "o.txt").write_text("o")
    (src / "link").symlink_to(tmp_path / "other", target_is_directory=True)
    (src / "r.txt").write_text("r")
    assert [f.rel for f in scan([str(src)], []).files] == ["r.txt"]
