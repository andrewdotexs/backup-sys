"""Avvio automatico del daemon all'accesso dell'utente.

Windows: valore ``backup-sys`` in ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
(nessun privilegio di amministratore richiesto, il daemon gira come utente normale).
Linux (sviluppo): file ``~/.config/autostart/backup-sys.desktop``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "backup-sys"


def command_line() -> str:
    if getattr(sys, "frozen", False):  # eseguibile PyInstaller: usa sempre la versione senza console
        gui = Path(sys.executable).with_name("backup-sys.exe")
        return f'"{gui if gui.exists() else sys.executable}" daemon'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else exe  # pythonw: nessuna finestra di console
    return f'"{interpreter}" -m backupsys daemon'


def _desktop_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "backup-sys.desktop"


def install() -> str:
    cmd = command_line()
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        import winreg

        # CreateKeyEx: su un profilo nuovo la chiave Run può non esistere ancora
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return cmd
    f = _desktop_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        f"[Desktop Entry]\nType=Application\nName=backup-sys\nExec={cmd}\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return cmd


def uninstall() -> None:
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            pass
        return
    f = _desktop_file()
    if f.exists():
        f.unlink()  # file di avvio automatico locale (ammesso dal test anti-cancellazione)


def is_installed() -> bool:
    if sys.platform == "win32":  # pragma: no cover - solo Windows
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, VALUE_NAME)
            return True
        except FileNotFoundError:
            return False
    return _desktop_file().exists()
