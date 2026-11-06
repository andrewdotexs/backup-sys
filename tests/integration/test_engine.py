from __future__ import annotations

import json
import os
from pathlib import Path

from backupsys.catalog import db
from backupsys.core.manifest import load_expected
from backupsys.security import crypto, secrets
from tests.conftest import Env


def test_first_run_copies_everything(env: Env) -> None:
    env.write("a.txt", "uno")
    env.write("sub/b.txt", "due")
    env.write("scarto.tmp", "x")
    r = env.run()
    assert r.status == db.OK
    assert (env.current() / "a.txt").read_text() == "uno"
    assert (env.current() / "sub" / "b.txt").read_text() == "due"
    assert not (env.current() / "scarto.tmp").exists()
    assert r.dests[0].copied == 2
    # mtime preservato
    assert os.stat(env.current() / "a.txt").st_mtime_ns == os.stat(env.src / "a.txt").st_mtime_ns


def test_second_run_skips_unchanged(env: Env) -> None:
    env.write("a.txt", "uno")
    env.run()
    r = env.run()
    assert r.dests[0].copied == 0 and r.dests[0].versioned == 0 and r.dests[0].skipped == 1


def test_modified_file_keeps_old_version(env: Env) -> None:
    env.write("a.txt", "versione 1")
    env.run()
    env.write("a.txt", "versione 2 più lunga")
    r = env.run()
    assert r.dests[0].versioned == 1
    assert (env.current() / "a.txt").read_text() == "versione 2 più lunga"
    olds = list(env.versions().rglob("a.txt"))
    assert len(olds) == 1 and olds[0].read_text() == "versione 1"


def test_file_deleted_on_pc_stays_on_backup(env: Env) -> None:
    p = env.write("a.txt", "uno")
    env.run()
    p.unlink()
    env.run()
    assert (env.current() / "a.txt").read_text() == "uno"


def test_file_deleted_by_user_on_backup_is_restored(env: Env) -> None:
    env.write("a.txt", "uno")
    env.write("d/b.txt", "due")
    env.run()
    (env.current() / "a.txt").unlink()
    import shutil

    shutil.rmtree(env.current() / "d")
    r = env.run()
    assert r.dests[0].copied == 2
    assert (env.current() / "a.txt").read_text() == "uno"
    assert (env.current() / "d" / "b.txt").read_text() == "due"


def test_type_conflict_file_becomes_directory(env: Env) -> None:
    p = env.write("x", "ero un file")
    env.run()
    p.unlink()
    env.write("x/dentro.txt", "ora sono una cartella")
    r = env.run()
    assert r.status == db.OK, r.message
    assert (env.current() / "x" / "dentro.txt").read_text() == "ora sono una cartella"
    archived = [f for f in env.versions().rglob("x") if f.is_file()]
    assert archived and archived[0].read_text() == "ero un file"


def test_type_conflict_directory_becomes_file(env: Env) -> None:
    env.write("y/f.txt", "contenuto")
    env.run()
    import shutil

    shutil.rmtree(env.src / "y")
    env.write("y", "ora file")
    r = env.run()
    assert r.status == db.OK, r.message
    assert (env.current() / "y").read_text() == "ora file"
    assert [f.read_text() for f in env.versions().rglob("f.txt")] == ["contenuto"]


def test_multiple_destinations(env2: Env) -> None:
    env2.write("a.txt", "uno")
    r = env2.run()
    assert r.status == db.OK
    for i in range(2):
        assert (env2.current(i) / "a.txt").read_text() == "uno"


def test_unplugged_destination_skipped(env2: Env) -> None:
    env2.write("a.txt", "uno")
    env2.disks[1].rename(env2.disks[1].with_name("staccato"))
    r = env2.run()
    statuses = {d.name: d.status for d in r.dests}
    assert statuses == {"Disco 0": db.OK, "Disco 1": db.SKIPPED}
    assert r.status == db.WARNING


def test_wrong_disk_refused(env: Env, tmp_path: Path) -> None:
    env.write("a.txt", "uno")
    env.run()
    assert env.config.destinations[0].volume_id
    # un altro disco montato sullo stesso percorso
    env.disks[0].rename(tmp_path / "disco-vero")
    env.disks[0].mkdir()
    r = env.run()
    assert r.dests[0].status == db.FAILED
    assert "non riconosciuto" in r.dests[0].message
    assert not (env.disks[0] / "backup-sys").exists()


def test_guard_pauses_then_override(env: Env) -> None:
    env.job.guard_min_files = 10
    for i in range(20):
        env.write(f"f{i}.txt", "originale")
    env.run()
    for i in range(20):
        env.write(f"f{i}.txt", "CIFRATO-DA-RANSOMWARE")
    r = env.run()
    assert r.status == db.PAUSED
    assert (env.current() / "f0.txt").read_text() == "originale"
    assert not env.versions().exists()
    r = env.run(override_guard=True, trigger="confirm")
    assert r.status == db.OK
    assert (env.current() / "f0.txt").read_text() == "CIFRATO-DA-RANSOMWARE"


def test_partial_file_from_interrupted_copy_is_reused(env: Env) -> None:
    env.write("a.txt", "uno")
    env.current().mkdir(parents=True)
    (env.current() / "a.txt.bsys-partial").write_text("spazzatura incompleta")
    r = env.run()
    assert r.status == db.OK
    assert (env.current() / "a.txt").read_text() == "uno"
    assert not (env.current() / "a.txt.bsys-partial").exists()


def test_manifest_records_hashes(env: Env) -> None:
    env.write("a.txt", "uno")
    env.run()
    env.write("a.txt", "due!")
    env.run()
    root = env.disks[0] / "backup-sys" / env.job.slug
    exp = load_expected(root / ".bsys" / "manifests")
    cur = [k for k in exp if k.startswith("current/") and k.endswith("a.txt")]
    ver = [k for k in exp if k.startswith("versions/") and k.endswith("a.txt")]
    assert len(cur) == 1 and len(ver) == 1
    import hashlib

    assert exp[cur[0]].sha256 == hashlib.sha256(b"due!").hexdigest()
    assert exp[ver[0]].sha256 == hashlib.sha256(b"uno").hexdigest()


def test_encrypted_destination(env: Env) -> None:
    dest = env.config.destinations[0]
    dest.encrypted = True
    keyfile = env.disks[0] / "backup-sys" / crypto.KEYFILE_NAME
    key = crypto.create_keyfile(keyfile, "una passphrase lunga")
    secrets.set_dest_key(dest.id, key)
    env.write("a.txt", "dato riservato")
    r = env.run()
    assert r.status == db.OK, r.message
    enc = env.current() / "a.txt.bsenc"
    assert enc.exists() and b"riservato" not in enc.read_bytes()
    r = env.run()
    assert r.dests[0].skipped == 1 and r.dests[0].copied == 0
    import io

    out = io.BytesIO()
    with enc.open("rb") as f:
        crypto.StreamCipher(key).decrypt_stream(f, out)
    assert out.getvalue() == b"dato riservato"


def test_encrypted_destination_without_key_fails(env: Env) -> None:
    env.config.destinations[0].encrypted = True
    env.write("a.txt", "x")
    r = env.run()
    assert r.dests[0].status == db.FAILED and "Chiave" in r.dests[0].message


def test_catalog_and_audit(env: Env) -> None:
    env.write("a.txt", "uno")
    r = env.run()
    row = env.catalog.run(r.run_id)
    assert row and row.status == db.OK and row.files_scanned == 1
    assert env.catalog.last_success(env.job.id, env.config.destinations[0].id) is not None
    actions = [e["action"] for e in env.audit.entries()]
    assert "run_started" in actions and "run_finished" in actions and "dest_bound" in actions
    assert env.audit.verify().ok


def test_marker_written(env: Env) -> None:
    env.write("a.txt", "uno")
    env.run()
    marker = json.loads((env.disks[0] / "backup-sys" / ".bsys-volume.json").read_text())
    assert marker["volume_id"] == env.config.destinations[0].volume_id
