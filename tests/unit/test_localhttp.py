import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backupsys import localhttp


class _H(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True, "pid": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:
        pass


def test_healthz_ignores_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regressione: con un proxy di sistema il servizio locale sembrava spento."""
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(var, "http://proxy-inesistente.invalid:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert localhttp.healthz(srv.server_address[1]) == {"ok": True, "pid": 1}
    finally:
        srv.shutdown()


def test_healthz_none_when_nobody_listens() -> None:
    s = HTTPServer(("127.0.0.1", 0), _H)
    port = s.server_address[1]
    s.server_close()
    assert localhttp.healthz(port, timeout=0.5) is None
