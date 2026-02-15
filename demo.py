"""
Wallpaper Sensi — Demo
======================
Quick visual test: injects a red-to-blue gradient behind the desktop icons.

Usage:
    python demo.py

Controls:
    Ctrl+C  or close the terminal to exit cleanly.
"""

import signal
import sys
import logging

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QLinearGradient, QColor, QPainter, QBrush
from PyQt6.QtWidgets import QApplication

from wallpaper_host import WallpaperHost

# ── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(name)-18s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("demo")


# ── Gradient painter callback ───────────────────────────────
def paint_gradient(painter: QPainter, w: int, h: int) -> None:
    """Draw a diagonal red → blue gradient filling the entire surface."""
    grad = QLinearGradient(QPointF(0, 0), QPointF(w, h))
    grad.setColorAt(0.0, QColor("#e74c3c"))   # red
    grad.setColorAt(0.5, QColor("#8e44ad"))   # purple
    grad.setColorAt(1.0, QColor("#2980b9"))   # blue
    painter.fillRect(0, 0, w, h, QBrush(grad))


# ── Main ────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)

    host = WallpaperHost()

    # ── Clean exit on Ctrl+C ────────────────────────────────
    def _shutdown(*_):
        log.info("Shutting down…")
        host.stop()
        app.quit()

    signal.signal(signal.SIGINT, _shutdown)

    # ── Inject & dock ───────────────────────────────────────
    if not host.start():
        log.error("Failed to dock — aborting.")
        sys.exit(1)

    host.set_paint_callback(paint_gradient)

    # Trigger a single repaint (no animation timer needed for
    # a static gradient — CPU & GPU stay at 0 %).
    if host.host_hwnd and host._widget:
        host._widget.update()

    log.info("Desktop host is running.  Press Ctrl+C to exit.")

    # ── Event-driven idle: uses the standard Qt / Win32
    #    message pump (GetMessage) — 0 % CPU at rest. ────────
    app.aboutToQuit.connect(host.stop)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
