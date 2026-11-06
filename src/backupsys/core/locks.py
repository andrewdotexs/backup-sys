"""Lock di processo per destinazione (file nella cartella dati locale, mai sul disco di backup).

Il lock è rilasciato dal sistema operativo alla chiusura del file/processo: nessun file da cancellare.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import IO

from backupsys import appdirs


class LockBusyError(Exception):
    pass


class DestinationLock:
    def __init__(self, dest_id: str) -> None:
        d = appdirs.data_dir() / "locks"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{dest_id}.lock"
        self._f: IO[bytes] | None = None

    def acquire(self) -> None:
        f = open(self.path, "a+b")  # noqa: SIM115
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            raise LockBusyError("Un altro backup sta già scrivendo su questa destinazione") from e
        self._f = f

    def release(self) -> None:
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        finally:
            self._f.close()
            self._f = None

    def __enter__(self) -> DestinationLock:
        self.acquire()
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.release()
