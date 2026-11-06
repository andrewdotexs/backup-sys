"""Richieste HTTP al servizio locale di backup-sys, SEMPRE senza proxy.

``urllib`` di default usa il proxy di sistema (variabili d'ambiente o impostazioni di Windows) anche
per 127.0.0.1: con un proxy configurato il servizio locale sembrerebbe spento.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthz(port: int, timeout: float = 2) -> dict[str, Any] | None:
    """Risposta di ``/healthz`` del servizio su ``port``, o None se nessuno risponde."""
    try:
        with _opener.open(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    return data if isinstance(data, dict) else None
