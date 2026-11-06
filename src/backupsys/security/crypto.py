"""Cifratura opzionale per destinazione (D4).

Formato file ``.bsenc`` (costruzione STREAM su AES-256-GCM):

    header  = b"BSE1" || nonce_prefix(8 byte casuali)
    chunk_i = AES-GCM(key, nonce = nonce_prefix || i (4 byte BE),
                      data = plaintext[i*CHUNK:(i+1)*CHUNK],
                      aad  = header || i (4 byte BE) || is_last (1 byte))

Il flag ``is_last`` nell'AAD impedisce troncamenti e riordini indetectabili.
La dimensione cifrata è deterministica (serve al confronto rapido senza decifrare):
``12 + n + 16 * max(1, ceil(n / CHUNK))``.

La chiave dati (32 byte casuali) è protetta sul disco da una chiave derivata dalla passphrase
con Argon2id (``.bsys-key.json``) e salvata nel Windows Credential Manager per i backup automatici.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as pysecrets
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"BSE1"
HEADER_LEN = 12
CHUNK = 1024 * 1024
TAG = 16
SUFFIX = ".bsenc"
KEYFILE_NAME = ".bsys-key.json"


class DecryptionError(Exception):
    pass


def encrypted_size(plain_size: int) -> int:
    chunks = max(1, -(-plain_size // CHUNK))
    return HEADER_LEN + plain_size + TAG * chunks


def _aad(header: bytes, index: int, last: bool) -> bytes:
    return header + index.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


@dataclass(frozen=True)
class StreamCipher:
    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError("La chiave deve essere di 32 byte (AES-256)")

    def encrypt_stream(self, fin: BinaryIO, fout: BinaryIO) -> tuple[str, int]:
        """Cifra ``fin`` in ``fout``. Restituisce (sha256 del plaintext, byte di plaintext)."""
        aes = AESGCM(self.key)
        prefix = pysecrets.token_bytes(8)
        header = MAGIC + prefix
        fout.write(header)
        h = hashlib.sha256()
        total = 0
        index = 0
        current = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK) if len(current) == CHUNK else b""
            last = not nxt
            h.update(current)
            total += len(current)
            nonce = prefix + index.to_bytes(4, "big")
            fout.write(aes.encrypt(nonce, current, _aad(header, index, last)))
            if last:
                break
            current = nxt
            index += 1
            if index >= 2**32:
                raise ValueError("File troppo grande per il formato BSE1")
        return h.hexdigest(), total

    def decrypt_stream(self, fin: BinaryIO, fout: BinaryIO | None) -> tuple[str, int]:
        """Decifra (e verifica) ``fin``. Se ``fout`` è None verifica soltanto. Restituisce (sha256, byte)."""
        aes = AESGCM(self.key)
        header = fin.read(HEADER_LEN)
        if len(header) != HEADER_LEN or header[:4] != MAGIC:
            raise DecryptionError("Intestazione non valida: non è un file backup-sys cifrato")
        prefix = header[4:]
        h = hashlib.sha256()
        total = 0
        index = 0
        current = fin.read(CHUNK + TAG)
        while True:
            nxt = fin.read(CHUNK + TAG) if len(current) == CHUNK + TAG else b""
            last = not nxt
            nonce = prefix + index.to_bytes(4, "big")
            try:
                plain = aes.decrypt(nonce, current, _aad(header, index, last))
            except InvalidTag as e:
                raise DecryptionError(f"File corrotto o chiave errata (blocco {index})") from e
            h.update(plain)
            total += len(plain)
            if fout is not None:
                fout.write(plain)
            if last:
                break
            current = nxt
            index += 1
        return h.hexdigest(), total


# --------------------------------------------------------------------------- gestione chiave

ARGON2_PARAMS = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}


def _derive(passphrase: str, salt: bytes, params: dict[str, int]) -> bytes:
    return hash_secret_raw(
        passphrase.encode("utf-8"),
        salt,
        time_cost=params["time_cost"],
        memory_cost=params["memory_cost"],
        parallelism=params["parallelism"],
        hash_len=32,
        type=Type.ID,
    )


def create_keyfile(path: Path, passphrase: str, params: dict[str, int] | None = None) -> bytes:
    """Genera una nuova chiave dati, la protegge con la passphrase e la scrive in ``path``."""
    if len(passphrase) < 12:
        raise ValueError("La passphrase deve avere almeno 12 caratteri")
    if path.exists():
        raise FileExistsError(f"Esiste già un file chiave: {path}")
    params = params or ARGON2_PARAMS
    data_key = pysecrets.token_bytes(32)
    salt = pysecrets.token_bytes(16)
    nonce = pysecrets.token_bytes(12)
    kek = _derive(passphrase, salt, params)
    wrapped = AESGCM(kek).encrypt(nonce, data_key, b"backup-sys-key-v1")
    doc = {
        "version": 1,
        "kdf": "argon2id",
        "params": params,
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "wrapped_key": base64.b64encode(wrapped).decode(),
        "key_check": hashlib.sha256(b"check" + data_key).hexdigest()[:16],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    return data_key


def unlock_keyfile(path: Path, passphrase: str) -> bytes:
    doc = json.loads(path.read_text(encoding="utf-8"))
    kek = _derive(passphrase, base64.b64decode(doc["salt"]), doc["params"])
    try:
        return AESGCM(kek).decrypt(
            base64.b64decode(doc["nonce"]), base64.b64decode(doc["wrapped_key"]), b"backup-sys-key-v1"
        )
    except InvalidTag as e:
        raise DecryptionError("Passphrase errata") from e


def key_matches_keyfile(path: Path, key: bytes) -> bool:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return bool(doc.get("key_check") == hashlib.sha256(b"check" + key).hexdigest()[:16])


def recovery_code(key: bytes) -> str:
    """Chiave in formato stampabile (base32 a gruppi di 4) per il recovery kit."""
    b32 = base64.b32encode(key).decode().rstrip("=")
    return "-".join(b32[i : i + 4] for i in range(0, len(b32), 4))


def key_from_recovery_code(code: str) -> bytes:
    s = code.replace("-", "").replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    key = base64.b32decode(s)
    if len(key) != 32:
        raise ValueError("Codice di recupero non valido")
    return key
