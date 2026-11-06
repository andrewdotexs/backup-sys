"""Autenticazione all'interfaccia: password locale (Argon2id), sessioni, CSRF, blocco tentativi."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backupsys import appdirs

MIN_PASSWORD_LEN = 8
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


class AuthStore:
    """Hash della password dell'interfaccia (mai la password in chiaro)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or appdirs.config_dir() / "auth.json"
        self._ph = PasswordHasher()

    @property
    def configured(self) -> bool:
        return self.path.exists()

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"La password deve avere almeno {MIN_PASSWORD_LEN} caratteri")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hash": self._ph.hash(password)}), encoding="utf-8")
        os.replace(tmp, self.path)

    def verify(self, password: str) -> bool:
        if not self.configured:
            return False
        h = json.loads(self.path.read_text(encoding="utf-8"))["hash"]
        try:
            self._ph.verify(h, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if self._ph.check_needs_rehash(h):
            self.set_password(password)
        return True


@dataclass
class Session:
    token: str
    csrf: str
    expires: float
    created: float = field(default_factory=time.time)


class SessionManager:
    def __init__(self, idle_minutes: int = 30) -> None:
        self.idle = idle_minutes * 60
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> Session:
        s = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time() + self.idle)
        with self._lock:
            self._sessions[s.token] = s
        return s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if s.expires < time.time():
                self._sessions.pop(token, None)
                return None
            s.expires = time.time() + self.idle  # scadenza a scorrimento
            return s

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


class LoginThrottle:
    """Dopo MAX_FAILURES tentativi falliti l'accesso è bloccato per LOCKOUT_SECONDS (raddoppiati ogni volta)."""

    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
        self.lockouts = 0
        self._lock = threading.Lock()

    def remaining_lock(self) -> int:
        return max(0, int(self.locked_until - time.time()))

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                self.lockouts += 1
                self.locked_until = time.time() + LOCKOUT_SECONDS * 2 ** (self.lockouts - 1)
                self.failures = 0

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.lockouts = 0
