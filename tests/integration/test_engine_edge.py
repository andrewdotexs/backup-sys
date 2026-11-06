from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from backupsys.catalog import db
from backupsys.core import engine as engine_mod
from backupsys.core import fsutil
from backupsys.core.engine import RunOptions
from backupsys.core.layout import DestLayout
from backupsys.core.locks import DestinationLock, LockBusyError
from backupsys.core.verify import verify
from backupsys.security import crypto, secrets
from tests.conftest import Env


def test_deep_compare_detects_same_size_and_mtime_change(env: Env) -> None:
    env.job.deep_compare = True
    p = env.write("a.txt", "AAAA")
    env.run()
    st = p.stat()
    p.write_text("BBBB")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    r = env.run()
    assert r.dests[0].versioned == 1
    assert (env.current() / "a.txt").read_text() == "BBBB"


def test_without_deep_compare_same_metadata_is_skipped(env: Env) -> None:
    p = env.write("a.txt", "AAAA")
    env.run()
    st = p.stat()
    p.write_text("BBBB")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert env.run().dests[0].skipped == 1


def test_lock_busy(env: Env) -> None:
    env.write("a.txt", "x")
    env.run()  # associa il disco
    dest = env.config.destinations[0]
    with DestinationLock(dest.id):
        with pytest.raises(LockBusyError):
            DestinationLock(dest.id).acquire()
        r = env.run()
        assert r.dests[0].status == db.FAILED and "altro backup" in r.dests[0].message


def test_cancel_before_start(env: Env) -> None:
    env.write("a.txt", "x")
    ev = threading.Event()
    ev.set()
    r = env.engine.run(env.job, env.config.destinations, RunOptions(), cancel=ev)
    assert r.status == db.CANCELLED


def test_insufficient_space(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.write("big.bin", b"x" * 1000)
    monkeypatch.setattr(engine_mod, "disk_usage", lambda p: (10**9, 10**9 - 10, 10))
    r = env.run()
    assert r.dests[0].status == db.FAILED and "Spazio insufficiente" in r.dests[0].message
    assert not (env.current() / "big.bin").exists()


def test_source_changed_during_copy(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.write("a.txt", "x")
    real_fstat = os.fstat

    class FakeStat:
        def __init__(self, st: os.stat_result) -> None:
            self.st_size = st.st_size + 1
            self.st_mtime_ns = st.st_mtime_ns
            self.st_atime_ns = st.st_atime_ns

    import backupsys.core.safe_writer as sw

    monkeypatch.setattr(sw.os, "fstat", lambda fd: FakeStat(real_fstat(fd)))
    r = env.run()
    assert r.status == db.WARNING and "durante la copia" in r.dests[0].errors[0][1]
    assert not (env.current() / "a.txt").exists()
    monkeypatch.setattr(sw.os, "fstat", real_fstat)
    assert env.run().status == db.OK and (env.current() / "a.txt").exists()


def test_locked_file_does_not_lose_old_version(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un file bloccato (es. Outlook aperto) non deve far sparire la versione già salvata."""
    env.write("posta.pst", "vecchia")
    env.run()
    env.write("posta.pst", "nuova versione")
    import builtins

    real_open = builtins.open

    def fake_open(file, mode="r", *a, **kw):  # type: ignore[no-untyped-def]
        if str(file).endswith("posta.pst") and "pc" in str(file) and "r" in mode:
            raise PermissionError(13, "Il file è in uso da un altro processo")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    r = env.run()
    monkeypatch.setattr(builtins, "open", real_open)
    assert r.status == db.WARNING
    assert (env.current() / "posta.pst").read_text() == "vecchia"


def test_verify_encrypted_and_unverifiable(env: Env) -> None:
    dest = env.config.destinations[0]
    dest.encrypted = True
    key = crypto.create_keyfile(env.disks[0] / "backup-sys" / crypto.KEYFILE_NAME, "passphrase lunghissima")
    secrets.set_dest_key(dest.id, key)
    env.write("a.txt", "segreto")
    env.run()
    layout = DestLayout(env.disks[0]).job(env.job.slug)
    (env.current() / "copiato-a-mano.txt").write_text("?")
    rep = verify(layout, crypto.StreamCipher(key))
    assert rep.ok == 1 and rep.unverifiable == 1 and rep.healthy
    enc = env.current() / "a.txt.bsenc"
    blob = bytearray(enc.read_bytes())
    blob[-1] ^= 1
    enc.write_bytes(bytes(blob))
    rep = verify(layout, crypto.StreamCipher(key))
    assert len(rep.corrupted) == 1
    rep = verify(layout, None)
    assert rep.errors


def test_verify_missing_file(env: Env) -> None:
    env.write("a.txt", "x")
    env.run()
    (env.current() / "a.txt").unlink()
    rep = verify(DestLayout(env.disks[0]).job(env.job.slug), None)
    assert len(rep.missing) == 1 and rep.healthy


def test_fsutil_helpers(tmp_path: Path) -> None:
    assert fsutil.human_bytes(512) == "512 B"
    assert fsutil.human_bytes(1536) == "1.5 KB"
    assert fsutil.human_bytes(5 * 2**40) == "5.0 TB"
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("a")
    b.write_text("b")
    with pytest.raises(FileExistsError):
        fsutil.rename_no_clobber(a, b)
    assert b.read_text() == "b"
    fsutil.rename_no_clobber(a, tmp_path / "c")
    assert (tmp_path / "c").read_text() == "a"
    assert fsutil.mtimes_equal(0, 1_999_999_999) and not fsutil.mtimes_equal(0, 2_000_000_001)
