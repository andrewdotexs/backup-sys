"""Icona nella tray di Windows: stato, apertura interfaccia, notifiche."""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def _icon_image() -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(30, 102, 196, 255))
    d.rectangle((18, 16, 46, 44), outline=(255, 255, 255, 255), width=4)  # disco
    d.polygon([(26, 26), (38, 26), (32, 36)], fill=(255, 255, 255, 255))  # freccia verso il disco
    d.rectangle((18, 46, 46, 50), fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, url: str, run_all: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self.url = url
        self.run_all = run_all
        self.on_quit = on_quit
        self._icon: Any = None

    @staticmethod
    def available() -> bool:
        try:
            import PIL  # noqa: F401
            import pystray  # noqa: F401
        except Exception:
            return False
        return True

    def notify(self, title: str, message: str) -> None:
        log.info("%s: %s", title, message)
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                log.debug("Notifica non mostrata", exc_info=True)

    def run(self) -> None:
        """Bloccante: va eseguito nel thread principale."""
        import pystray

        def open_ui(_icon: Any = None, _item: Any = None) -> None:
            webbrowser.open(self.url)

        def run_all(_icon: Any = None, _item: Any = None) -> None:
            threading.Thread(target=self.run_all, daemon=True).start()

        def quit_(icon: Any, _item: Any = None) -> None:
            icon.stop()
            self.on_quit()

        menu = pystray.Menu(
            pystray.MenuItem("Apri backup-sys", open_ui, default=True),
            pystray.MenuItem("Esegui tutti i backup ora", run_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Esci", quit_),
        )
        self._icon = pystray.Icon("backup-sys", _icon_image(), "backup-sys", menu)
        self._icon.run()
