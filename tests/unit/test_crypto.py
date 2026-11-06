import io
import secrets
from pathlib import Path

import pytest

from backupsys.security import crypto


@pytest.mark.parametrize("size", [0, 1, 1000, crypto.CHUNK - 1, crypto.CHUNK, crypto.CHUNK + 1, 3 * crypto.CHUNK])
def test_roundtrip_and_size(size: int) -> None:
    c = crypto.StreamCipher(secrets.token_bytes(32))
    data = secrets.token_bytes(size)
    enc = io.BytesIO()
    sha, n = c.encrypt_stream(io.BytesIO(data), enc)
    assert n == size
    assert len(enc.getvalue()) == crypto.encrypted_size(size)
    out = io.BytesIO()
    sha2, n2 = c.decrypt_stream(io.BytesIO(enc.getvalue()), out)
    assert out.getvalue() == data and sha == sha2 and n2 == size


def _enc(c: crypto.StreamCipher, data: bytes) -> bytes:
    buf = io.BytesIO()
    c.encrypt_stream(io.BytesIO(data), buf)
    return buf.getvalue()


def test_tamper_detected() -> None:
    c = crypto.StreamCipher(secrets.token_bytes(32))
    blob = bytearray(_enc(c, b"x" * 5000))
    blob[100] ^= 1
    with pytest.raises(crypto.DecryptionError):
        c.decrypt_stream(io.BytesIO(bytes(blob)), None)


def test_truncation_detected() -> None:
    c = crypto.StreamCipher(secrets.token_bytes(32))
    blob = _enc(c, secrets.token_bytes(2 * crypto.CHUNK + 10))
    truncated = blob[: crypto.HEADER_LEN + crypto.CHUNK + crypto.TAG]  # solo il primo blocco
    with pytest.raises(crypto.DecryptionError):
        c.decrypt_stream(io.BytesIO(truncated), None)


def test_wrong_key() -> None:
    blob = _enc(crypto.StreamCipher(secrets.token_bytes(32)), b"segreto")
    with pytest.raises(crypto.DecryptionError):
        crypto.StreamCipher(secrets.token_bytes(32)).decrypt_stream(io.BytesIO(blob), None)


def test_keyfile_and_recovery(tmp_path: Path) -> None:
    kf = tmp_path / "k.json"
    key = crypto.create_keyfile(kf, "passphrase-lunga-123")
    assert crypto.unlock_keyfile(kf, "passphrase-lunga-123") == key
    assert crypto.key_matches_keyfile(kf, key)
    with pytest.raises(crypto.DecryptionError):
        crypto.unlock_keyfile(kf, "sbagliata-sbagliata")
    with pytest.raises(FileExistsError):
        crypto.create_keyfile(kf, "passphrase-lunga-123")
    code = crypto.recovery_code(key)
    assert crypto.key_from_recovery_code(code) == key
    with pytest.raises(ValueError):
        crypto.create_keyfile(tmp_path / "k2.json", "corta")
