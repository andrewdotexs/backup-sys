# PyInstaller: produce dist/backup-sys/ con
#   backup-sys.exe      -> senza console: doppio clic avvia il servizio e apre l'interfaccia
#   backup-sys-cli.exe  -> con console: comandi da terminale (run, status, verify, decrypt, ...)
# Uso (su Windows, dalla radice del repository):  uv run pyinstaller scripts/backup-sys.spec --noconfirm
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src" / "backupsys"

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("backupsys")
    + ["keyring.backends.Windows", "pystray._win32", "argon2._ffi"]
)
datas = [
    (str(SRC / "ui" / "templates"), "backupsys/ui/templates"),
    (str(SRC / "ui" / "static"), "backupsys/ui/static"),
]

a = Analysis([str(SRC / "__main__.py")], pathex=[str(ROOT / "src")], datas=datas, hiddenimports=hidden)
pyz = PYZ(a.pure)

gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="backup-sys", console=False,
          icon=str(ROOT / "scripts" / "backup-sys.ico"))
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="backup-sys-cli", console=True,
          icon=str(ROOT / "scripts" / "backup-sys.ico"))
coll = COLLECT(gui, cli, a.binaries, a.datas, name="backup-sys")
