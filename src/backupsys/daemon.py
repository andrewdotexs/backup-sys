"""Processo in background: worker dei backup + scheduler + interfaccia web + icona tray."""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

from backupsys import appdirs, localhttp


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(appdirs.log_dir() / "backup-sys.log", maxBytes=5 * 2**20, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(sh)


def _error_box(message: str) -> None:
    """Finestra di errore su Windows (l'eseguibile senza console non avrebbe dove scrivere)."""
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "backup-sys", 0x10)


def already_running(port: int) -> bool:
    info = localhttp.healthz(port)
    return bool(info and info.get("ok"))


def run(port: int | None = None, tray: bool = True, open_browser: bool = False) -> None:
    import uvicorn

    from backupsys.scheduler.loop import Scheduler
    from backupsys.service import BackupService
    from backupsys.tray import Tray
    from backupsys.ui.app import create_app

    setup_logging()
    log = logging.getLogger("backupsys.daemon")
    service = BackupService()
    port = port or service.config.ui.port
    url = f"http://127.0.0.1:{port}/"
    if already_running(port):
        log.info("backup-sys è già in esecuzione: apro l'interfaccia")
        webbrowser.open(url)
        return

    scheduler = Scheduler(service)
    try:
        app = create_app(service, scheduler, port=port)
    except RuntimeError as e:  # installazione danneggiata: messaggio comprensibile invece del traceback
        log.error("%s", e)
        _error_box(str(e))
        return
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    server_thread = threading.Thread(target=server.run, name="web", daemon=True)

    service.start()
    scheduler.start()
    server_thread.start()
    service.audit.log("daemon_started", port=port)
    log.info("backup-sys avviato: %s", url)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    def shutdown() -> None:
        log.info("Arresto in corso…")
        scheduler.stop()
        service.stop()
        server.should_exit = True
        service.audit.log("daemon_stopped")

    def run_all() -> None:
        for j in service.config.jobs:
            if j.enabled:
                service.enqueue_backup(j.id)

    try:
        if tray and Tray.available():
            t = Tray(url, run_all, shutdown)
            service.notifier = t.notify
            t.run()  # bloccante fino a "Esci"
        else:
            while server_thread.is_alive():
                server_thread.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        if not server.should_exit:
            shutdown()
        server_thread.join(5)
