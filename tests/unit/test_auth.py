import time

import pytest

from backupsys.security.auth import AuthStore, LoginThrottle, SessionManager


def test_password_roundtrip() -> None:
    a = AuthStore()
    assert not a.configured and not a.verify("x")
    with pytest.raises(ValueError):
        a.set_password("corta")
    a.set_password("password-sicura")
    assert a.configured and a.verify("password-sicura") and not a.verify("sbagliata")
    assert "password-sicura" not in a.path.read_text()


def test_sessions_expire() -> None:
    m = SessionManager(idle_minutes=5)
    s = m.create()
    assert m.get(s.token) is s
    s.expires = time.time() - 1
    assert m.get(s.token) is None
    assert m.get(None) is None


def test_throttle_lockout() -> None:
    t = LoginThrottle()
    for _ in range(5):
        t.failure()
    assert t.remaining_lock() > 0
