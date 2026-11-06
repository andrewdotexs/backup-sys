"""Icona nella tray di Windows: stato e avanzamento del backup, apertura interfaccia, notifiche.

Durante un backup l'icona mostra la percentuale (e una barra), il tooltip il dettaglio
(«Documenti → USB: 42%») e il menu una riga di stato. Le funzioni di disegno e di testo sono pure,
così si possono provare anche senza la tray di Windows.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

SIZE = 64
BLUE = (30, 102, 196, 255)
GREEN = (46, 204, 113, 255)
ORANGE = (243, 156, 18, 255)
WHITE = (255, 255, 255, 255)
TOOLTIP_MAX = 120  # Windows tronca il tooltip delle icone della tray a 127 caratteri

PHASE_TEXT = {"scan": "analisi dei file", "plan": "confronto con il backup"}

Snapshot = dict[str, Any]


def progress_percent(snap: Snapshot) -> int | None:
    """Percentuale (0-100) della copia in corso, o None se non c'è una copia in corso."""
    run = snap.get("running")
    if not isinstance(run, dict) or run.get("kind") != "backup" or run.get("phase") != "copy":
        return None
    if run.get("bytes_total"):
        pct = 100 * run["bytes_done"] / run["bytes_total"]
    elif run.get("files_total"):
        pct = 100 * run["files_done"] / run["files_total"]
    else:
        return 100
    return max(0, min(100, int(pct)))


def status_text(snap: Snapshot) -> str:
    run = snap.get("running")
    if not isinstance(run, dict):
        queued = snap.get("queued") or []
        return f"backup-sys — {len(queued)} backup in coda" if queued else "backup-sys — nessun backup in corso"
    if run.get("kind") == "verify":
        text = f"backup-sys — {run.get('label') or 'verifica di integrità'} in corso"
    else:
        where = f" → {run['dest']}" if run.get("dest") else ""
        pct = progress_percent(snap)
        what = f"{pct}%" if pct is not None else PHASE_TEXT.get(str(run.get("phase")), "in corso") + "…"
        text = f"«{run.get('job_name', '')}»{where}: {what}"
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"


def _font(size: int) -> Any:
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_icon(percent: int | None = None, busy: bool = False) -> Any:
    """Icona 64×64: disco (inattivo), disco con pallino arancione (analisi/verifica), percentuale + barra (copia)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=12, fill=BLUE)
    if percent is None:
        d.rectangle((18, 16, 46, 44), outline=WHITE, width=4)  # disco
        d.polygon([(26, 26), (38, 26), (32, 36)], fill=WHITE)  # freccia verso il disco
        d.rectangle((18, 46, 46, 50), fill=WHITE)
        if busy:
            d.ellipse((42, 4, 60, 22), fill=ORANGE, outline=WHITE, width=2)
        return img
    text = str(percent)
    font = _font(38 if len(text) <= 2 else 27)  # grande: a 16 px (tray al 100%) deve restare leggibile
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((SIZE - w) / 2 - box[0], (44 - h) / 2 - box[1] + 2), text, font=font, fill=WHITE)
    d.rectangle((5, 45, 59, 59), fill=WHITE)  # barra di avanzamento
    if percent > 0:
        d.rectangle((8, 48, 8 + int(48 * percent / 100), 56), fill=GREEN)
    if percent < 100:
        d.rectangle((8 + int(48 * percent / 100) + 1, 48, 56, 56), fill=(200, 210, 225, 255))
    return img


def _icon_image() -> Any:
    return render_icon()


class Tray:
    def __init__(
        self,
        url: str,
        run_all: Callable[[], None],
        on_quit: Callable[[], None],
        status: Callable[[], Snapshot] | None = None,
        interval: float = 1.0,
    ) -> None:
        self.url = url
        self.run_all = run_all
        self.on_quit = on_quit
        self.status = status
        self.interval = interval
        self._icon: Any = None
        self._stop = threading.Event()
        self._menu_status = "Nessun backup in corso"

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

    def _updater(self) -> None:
        """Aggiorna icona, tooltip e menu solo quando lo stato cambia (niente sfarfallio)."""
        last: tuple[int | None, bool, str] | None = None
        while not self._stop.wait(self.interval):
            if self.status is None or self._icon is None:
                continue
            try:
                snap = self.status()
                pct = progress_percent(snap)
                busy = snap.get("running") is not None
                text = status_text(snap)
                state = (pct, busy, text)
                if state == last:
                    continue
                if last is None or (pct, busy) != last[:2]:
                    self._icon.icon = render_icon(pct, busy)
                self._icon.title = text
                self._menu_status = text.removeprefix("backup-sys — ").capitalize() if not busy else text
                self._icon.update_menu()
                last = state
            except Exception:
                log.debug("Aggiornamento della tray non riuscito", exc_info=True)

    def run(self) -> None:
        """Bloccante: va eseguito nel thread principale."""
        import pystray

        def open_ui(_icon: Any = None, _item: Any = None) -> None:
            webbrowser.open(self.url)

        def run_all(_icon: Any = None, _item: Any = None) -> None:
            threading.Thread(target=self.run_all, daemon=True).start()

        def quit_(icon: Any, _item: Any = None) -> None:
            self._stop.set()
            icon.stop()
            self.on_quit()

        def setup(icon: Any) -> None:
            icon.visible = True
            threading.Thread(target=self._updater, name="tray-updater", daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self._menu_status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Apri backup-sys", open_ui, default=True),
            pystray.MenuItem("Esegui tutti i backup ora", run_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Esci", quit_),
        )
        self._icon = pystray.Icon("backup-sys", render_icon(), "backup-sys", menu)
        self._icon.run(setup=setup)
