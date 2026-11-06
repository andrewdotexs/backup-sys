"""Rinomine e spostamenti sul PC: riconosciuti dal contenuto, collegati senza ricopiare (B),
con la storia delle versioni che segue il nuovo nome (C)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from backupsys.catalog import db
from backupsys.catalog.db import Catalog
from backupsys.core import restore, retention
from backupsys.core.layout import DestLayout
from backupsys.core.manifest import load_renames
from backupsys.security import crypto, secrets
from tests.conftest import Env


def _rename(env: Env, old: str, new: str) -> None:
    dst = env.src / new
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(env.src / old, dst)  # come Esplora file: data di modifica invariata


def _layout(env: Env):  # type: ignore[no-untyped-def]
    return DestLayout(env.disks[0]).job(env.job.slug)


def _key(env: Env) -> str:
    from backupsys.core.fsutil import source_key

    return source_key(str(env.src))


def test_rename_is_linked_not_copied(env: Env) -> None:
    env.write("FILE.txt", "contenuto importante " * 100)
    env.run()
    _rename(env, "FILE.txt", "FILE_MOD.txt")
    r = env.run()
    d = r.dests[0]
    assert r.status == db.OK and d.linked == 1 and d.copied == 0 and d.bytes == 0
    old, new = env.current() / "FILE.txt", env.current() / "FILE_MOD.txt"
    assert old.exists() and new.exists()  # il vecchio nome resta (mai cancellare)
    assert os.path.samefile(old, new)  # stesso dato su disco: nessuno spazio in più
    assert "rinominati" in d.message
    renames = load_renames(_layout(env).manifests)
    assert renames == {f"current/{_key(env)}/FILE_MOD.txt": f"current/{_key(env)}/FILE.txt"}


def test_move_to_other_folder_is_linked(env: Env) -> None:
    env.write("bozze/relazione.docx", "x" * 5000)
    env.run()
    _rename(env, "bozze/relazione.docx", "archivio/2025/relazione.docx")
    d = env.run().dests[0]
    assert d.linked == 1 and d.copied == 0
    assert (env.current() / "archivio" / "2025" / "relazione.docx").read_text() == "x" * 5000


def test_rename_and_modify_is_a_normal_copy(env: Env) -> None:
    env.write("FILE.txt", "prima")
    env.run()
    (env.src / "FILE.txt").unlink()
    env.write("FILE_MOD.txt", "dopo, diverso")
    d = env.run().dests[0]
    assert d.linked == 0 and d.copied == 1


def test_same_size_and_date_but_different_content_is_not_linked(env: Env) -> None:
    p = env.write("a.bin", b"A" * 1000)
    env.run()
    st = p.stat()
    p.unlink()
    q = env.src / "b.bin"
    q.write_bytes(b"B" * 1000)
    os.utime(q, ns=(st.st_atime_ns, st.st_mtime_ns))  # stessi indizi, contenuto diverso
    d = env.run().dests[0]
    assert d.linked == 0 and d.copied == 1
    assert (env.current() / "b.bin").read_bytes() == b"B" * 1000


def test_copy_with_original_still_on_pc_is_copied(env: Env) -> None:
    """Non è una rinomina: l'originale è ancora sul PC (niente orfano da collegare)."""
    import shutil

    env.write("a.txt", "dato " * 50)
    env.run()
    shutil.copy2(env.src / "a.txt", env.src / "copia di a.txt")
    d = env.run().dests[0]
    assert d.linked == 0 and d.copied == 1


def test_modifying_renamed_file_does_not_touch_old_name(env: Env) -> None:
    env.write("FILE.txt", "versione 1")
    env.run()
    _rename(env, "FILE.txt", "FILE_MOD.txt")
    env.run()
    env.write("FILE_MOD.txt", "versione 2 dopo la rinomina")
    d = env.run().dests[0]
    assert d.versioned == 1
    assert (env.current() / "FILE_MOD.txt").read_text() == "versione 2 dopo la rinomina"
    assert (env.current() / "FILE.txt").read_text() == "versione 1"  # nessuna "contaminazione"


def test_history_follows_renames(env: Env) -> None:
    env.write("FILE.txt", "v1")
    env.run()
    env.write("FILE.txt", "v2 più lunga")
    env.run()
    _rename(env, "FILE.txt", "FILE_MOD.txt")
    env.run()
    env.write("FILE_MOD.txt", "v3 col nuovo nome")
    env.run()
    _rename(env, "FILE_MOD.txt", "FILE_FINALE.txt")
    env.run()

    h = restore.file_history(_layout(env), f"{_key(env)}/FILE_FINALE.txt")
    contents = []
    for v in h.versions:
        root = _layout(env).current if v.area == "current" else _layout(env).versions / v.area.split("/")[1]
        contents.append((root.joinpath(*v.rel.split("/")).read_text(encoding="utf-8"), v.former_name))
    assert contents[0] == ("v3 col nuovo nome", None)  # copia corrente del nome attuale
    texts = [c for c, _ in contents]
    assert {"v1", "v2 più lunga", "v3 col nuovo nome"} <= set(texts)
    assert ("v2 più lunga", "FILE_MOD.txt") in contents or ("v2 più lunga", "FILE.txt") in contents
    assert ("v1", "FILE.txt") in contents
    old = restore.file_history(_layout(env), f"{_key(env)}/FILE.txt")
    assert old.renamed_to == ["FILE_MOD.txt"]


def test_encrypted_destination_rename(env: Env) -> None:
    dest = env.config.destinations[0]
    dest.encrypted = True
    key = crypto.create_keyfile(env.disks[0] / "backup-sys" / crypto.KEYFILE_NAME, "passphrase lunghissima")
    secrets.set_dest_key(dest.id, key)
    env.write("segreto.txt", "riservato " * 30)
    env.run()
    _rename(env, "segreto.txt", "segreto-2025.txt")
    d = env.run().dests[0]
    assert d.linked == 1
    import io

    out = io.BytesIO()
    with (env.current() / "segreto-2025.txt.bsenc").open("rb") as f:
        crypto.StreamCipher(key).decrypt_stream(f, out)
    assert out.getvalue() == ("riservato " * 30).encode()


def test_falls_back_to_copy_without_hardlink_support(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.write("FILE.txt", "dato " * 40)
    env.run()
    _rename(env, "FILE.txt", "FILE_MOD.txt")

    def no_link(*a: object, **k: object) -> None:
        raise OSError(1, "Operation not permitted (exFAT)")

    import backupsys.core.safe_writer as sw

    monkeypatch.setattr(sw.os, "link", no_link)
    d = env.run().dests[0]
    assert d.linked == 0 and d.copied == 1 and d.status == db.OK
    assert not os.path.samefile(env.current() / "FILE.txt", env.current() / "FILE_MOD.txt")


def test_space_measure_counts_hardlinked_data_once(env: Env) -> None:
    big = b"z" * (2 * retention.LINK_CHECK_MIN)
    env.write("video.mp4", big)
    env.run()
    _rename(env, "video.mp4", "vacanze.mp4")
    env.run()
    total, _versions = retention.measure(DestLayout(env.disks[0]))
    assert len(big) <= total < 2 * len(big)


def test_catalog_migration_adds_linked_column(tmp_path: Path) -> None:
    path = tmp_path / "vecchio.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE run_dests (run_id INTEGER NOT NULL, dest_id TEXT NOT NULL, dest_name TEXT NOT NULL, "
        "status TEXT NOT NULL, copied INTEGER DEFAULT 0, versioned INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, "
        "bytes INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, pruned_versions INTEGER DEFAULT 0, "
        "pruned_bytes INTEGER DEFAULT 0, message TEXT DEFAULT '', PRIMARY KEY (run_id, dest_id))"
    )
    con.commit()
    con.close()
    cat = Catalog(path)
    cat.record_dest(1, "d", "Disco", status="ok", linked=3)
    assert cat.run_dests(1)[0]["linked"] == 3
