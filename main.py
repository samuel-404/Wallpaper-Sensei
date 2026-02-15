"""
Wallpaper Sensi — Main Application
===================================
Professional desktop entry-point with:
  • Single-instance enforcement (QSharedMemory + QLocalServer IPC)
  • System tray icon (Open Gallery / Pause / Exit)
  • Hide-to-tray on window close (setQuitOnLastWindowClosed=False)
  • WebEngine wallpaper loading (Mond Clock)

Usage:
    python main.py
"""

from __future__ import annotations

import os
import sys

# ── GPU rasterisation for WebEngine (must precede Qt imports) ──
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-gpu-rasterization --enable-native-gpu-memory-buffers",
)

import json
import logging
import signal

from PyQt6.QtCore import Qt, QSharedMemory, QUrl
from PyQt6.QtGui import QIcon, QAction, QPalette, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QVBoxLayout,
    QLabel,
    QStyle,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from wallpaper_host import WallpaperHost
from taskbar_controller import TaskbarController

# ── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(name)-18s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("WallpaperSensei")

# ── Application key (shared memory + IPC) ───────────────────
APP_KEY = "WallpaperSensei_SingleInstance_v1"

# ── Paths ───────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
WALLPAPERS_DIR = os.path.join(BASE_DIR, "wallpapers")
SETTINGS_FILE  = os.path.join(BASE_DIR, "settings.json")


# ═════════════════════════════════════════════════════════════
#  GalleryWindow — placeholder main window (hide-to-tray)
# ═════════════════════════════════════════════════════════════
class GalleryWindow(QMainWindow):
    """
    Main window for the wallpaper gallery.
    Hidden by default — the app lives in the system tray.
    Closing this window hides it to the tray instead of quitting.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wallpaper Sensei")
        self.setMinimumSize(800, 600)
        self._build_ui()
        self._apply_dark_theme()

    # ── UI ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        from discover_tab import DiscoverTab

        self._discover = DiscoverTab()
        self.setCentralWidget(self._discover)

    # ── Dark palette ────────────────────────────────────────
    def _apply_dark_theme(self) -> None:
        p = QPalette()
        p.setColor(QPalette.ColorRole.Window, QColor(28, 28, 32))
        p.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
        p.setColor(QPalette.ColorRole.Base, QColor(22, 22, 26))
        p.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 40))
        p.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
        p.setColor(QPalette.ColorRole.Button, QColor(42, 42, 48))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
        p.setColor(QPalette.ColorRole.Highlight, QColor(100, 60, 220))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        self.setPalette(p)

    # ── Close → hide to tray ────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802
        """Override: hide to tray instead of quitting."""
        event.ignore()
        self.hide()
        log.info("Gallery hidden to system tray.")


# ═════════════════════════════════════════════════════════════
#  WallpaperSenseiApp — application controller
# ═════════════════════════════════════════════════════════════
class WallpaperSenseiApp:
    """
    Wires together all modules:
      WallpaperHost ← WebEngine (Mond Clock)
      TaskbarController
      GalleryWindow
      QSystemTrayIcon
    """

    def __init__(self) -> None:
        # ── QApplication ────────────────────────────────────
        self.app = QApplication(sys.argv)
        self.app.setApplicationName("Wallpaper Sensei")
        self.app.setQuitOnLastWindowClosed(False)

        # ── Single-instance guard ───────────────────────────
        self._shared_mem = QSharedMemory(APP_KEY)
        self._local_server: QLocalServer | None = None

        if not self._acquire_lock():
            self._signal_existing_instance()
            sys.exit(0)

        self._start_ipc_server()

        # ── Core modules ────────────────────────────────────
        self.host = WallpaperHost()
        self.taskbar = TaskbarController()
        self.gallery = GalleryWindow()

        # ── Connect Discover tab signals ─────────────────────
        self.gallery._discover.wallpaper_apply.connect(
            self._on_wallpaper_apply
        )

        # ── System tray ─────────────────────────────────────
        self._paused = False
        self._init_tray()

        # ── Launch wallpaper ────────────────────────────────
        self._launch_wallpaper()

        # ── Ctrl+C exit ─────────────────────────────────────
        signal.signal(signal.SIGINT, lambda *_: self._exit())

    # ─────────────────────────────────────────────────────────
    #  Settings persistence
    # ─────────────────────────────────────────────────────────
    def _load_settings(self) -> dict:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self, **updates) -> None:
        settings = self._load_settings()
        settings.update(updates)
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            log.info("Settings saved: %s", list(updates.keys()))
        except Exception as exc:
            log.error("Failed to save settings: %s", exc)

    # ─────────────────────────────────────────────────────────
    #  Single Instance (QSharedMemory + QLocalServer IPC)
    # ─────────────────────────────────────────────────────────
    def _acquire_lock(self) -> bool:
        """
        Try to create shared memory.  If it already exists,
        another instance owns it → return False.
        """
        # Windows: stale shared memory can survive a crash — detach first.
        if self._shared_mem.attach():
            self._shared_mem.detach()

        if self._shared_mem.create(1):
            log.info("Single-instance lock acquired.")
            return True

        log.warning("Another instance is already running.")
        return False

    def _signal_existing_instance(self) -> None:
        """Send a 'show' command to the running instance via QLocalSocket."""
        sock = QLocalSocket()
        sock.connectToServer(APP_KEY)
        if sock.waitForConnected(1000):
            sock.write(b"show")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
            log.info("Signalled existing instance to show gallery.")

    def _start_ipc_server(self) -> None:
        """Listen for 'show' commands from duplicate launches."""
        self._local_server = QLocalServer()
        QLocalServer.removeServer(APP_KEY)  # clean up stale socket
        if self._local_server.listen(APP_KEY):
            self._local_server.newConnection.connect(self._on_ipc_connection)
            log.info("IPC server listening on '%s'.", APP_KEY)

    def _on_ipc_connection(self) -> None:
        conn = self._local_server.nextPendingConnection()
        if conn:
            conn.waitForReadyRead(500)
            self._show_gallery()
            conn.disconnectFromClient()
            conn.deleteLater()

    # ─────────────────────────────────────────────────────────
    #  System Tray
    # ─────────────────────────────────────────────────────────
    def _init_tray(self) -> None:
        self._tray = QSystemTrayIcon(self.app)

        # Icon — custom file or platform default
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "icon.png"
        )
        if os.path.exists(icon_path):
            self._tray.setIcon(QIcon(icon_path))
        else:
            self._tray.setIcon(
                self.app.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon)
            )

        self._tray.setToolTip("Wallpaper Sensei")

        # ── Context menu ────────────────────────────────────
        menu = QMenu()

        open_act = QAction("🎨 Open Gallery", self.app)
        open_act.triggered.connect(self._show_gallery)
        menu.addAction(open_act)

        menu.addSeparator()

        self._pause_act = QAction("⏸ Pause Wallpaper", self.app)
        self._pause_act.triggered.connect(self._toggle_pause)
        menu.addAction(self._pause_act)

        menu.addSeparator()

        exit_act = QAction("✕ Exit", self.app)
        exit_act.triggered.connect(self._exit)
        menu.addAction(exit_act)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        log.info("System tray icon ready.")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_gallery()

    # ─────────────────────────────────────────────────────────
    #  Actions
    # ─────────────────────────────────────────────────────────
    def _show_gallery(self) -> None:
        self.gallery.show()
        self.gallery.raise_()
        self.gallery.activateWindow()

    def _on_wallpaper_apply(self, data: dict) -> None:
        """
        User clicked 'Apply' on a card.
        Resolves the wallpaper folder, ensures index.html exists
        (auto-generates an mp4 wrapper if needed), then loads it.
        """
        slug = data.get(
            "slug", data.get("title", "").lower().replace(" ", "_")
        )
        wp_dir = os.path.join(WALLPAPERS_DIR, slug)

        if not os.path.isdir(wp_dir):
            log.warning("Wallpaper folder not found: %s", wp_dir)
            return

        html = os.path.join(wp_dir, "index.html")

        # If no index.html, look for an mp4 and generate a wrapper
        if not os.path.exists(html):
            mp4 = self._find_mp4(wp_dir)
            if mp4:
                self._generate_video_wrapper(wp_dir, mp4)
                html = os.path.join(wp_dir, "index.html")
                log.info("Auto-generated index.html wrapper for %s", mp4)
            else:
                log.warning("No index.html or mp4 in: %s", wp_dir)
                return

        self.host.load_web_content(QUrl.fromLocalFile(html))
        self._save_settings(active_wallpaper=slug)
        log.info("Applied wallpaper: %s → %s", slug, html)

    @staticmethod
    def _find_mp4(folder: str) -> str | None:
        """Return the first .mp4 filename in *folder*, or None."""
        for f in os.listdir(folder):
            if f.lower().endswith(".mp4"):
                return f
        return None

    @staticmethod
    def _generate_video_wrapper(folder: str, video_name: str) -> None:
        """Create a minimal index.html that loops an mp4 full-screen."""
        html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<style>
  * {{ margin: 0; padding: 0; overflow: hidden; }}
  body {{ background: #000; }}
  video {{
    position: fixed; top: 50%; left: 50%;
    min-width: 100vw; min-height: 100vh;
    transform: translate(-50%, -50%);
    object-fit: cover;
  }}
</style>
</head>
<body>
  <video autoplay muted loop playsinline>
    <source src="{video_name}" type="video/mp4"/>
  </video>
</body>
</html>
"""
        with open(
            os.path.join(folder, "index.html"), "w", encoding="utf-8"
        ) as f:
            f.write(html)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.host.set_rendering_state(not self._paused)

        # Freeze / thaw WebEngine to save CPU + GPU
        wv = self.host._web_view
        if wv is not None:
            try:
                from PyQt6.QtWebEngineCore import QWebEnginePage

                state = (
                    QWebEnginePage.LifecycleState.Frozen
                    if self._paused
                    else QWebEnginePage.LifecycleState.Active
                )
                wv.page().setLifecycleState(state)
            except Exception:
                # Fallback: just toggle visibility
                wv.setVisible(not self._paused)

        self._pause_act.setText(
            "▶ Resume Wallpaper" if self._paused else "⏸ Pause Wallpaper"
        )
        log.info("Wallpaper %s.", "paused" if self._paused else "resumed")

    # ─────────────────────────────────────────────────────────
    #  Wallpaper bootstrap
    # ─────────────────────────────────────────────────────────
    def _launch_wallpaper(self) -> None:
        if not self.host.start():
            log.error("Failed to dock wallpaper host — aborting wallpaper.")
            return

        # Try to restore the last active wallpaper from settings
        settings = self._load_settings()
        saved_slug = settings.get("active_wallpaper")
        if saved_slug:
            wp_dir = os.path.join(WALLPAPERS_DIR, saved_slug)
            if os.path.isdir(wp_dir):
                html = os.path.join(wp_dir, "index.html")
                if not os.path.exists(html):
                    mp4 = self._find_mp4(wp_dir)
                    if mp4:
                        self._generate_video_wrapper(wp_dir, mp4)
                        html = os.path.join(wp_dir, "index.html")
                if os.path.exists(html):
                    self.host.load_web_content(QUrl.fromLocalFile(html))
                    log.info("Restored saved wallpaper: %s", saved_slug)
                    return

        # Fallback: load mond_clock
        self._load_mond_clock()

    def _load_mond_clock(self) -> None:
        html = os.path.join(WALLPAPERS_DIR, "mond_clock", "index.html")
        if os.path.exists(html):
            self.host.load_web_content(QUrl.fromLocalFile(html))
            log.info("Mond Clock wallpaper loaded via WebEngine.")
        else:
            log.warning("Mond Clock not found at: %s", html)

    # ─────────────────────────────────────────────────────────
    #  Clean shutdown
    # ─────────────────────────────────────────────────────────
    def _exit(self) -> None:
        log.info("Shutting down Wallpaper Sensei…")
        self.host.unload_web_content()
        self.taskbar.restore()
        self.host.stop()
        self._tray.hide()
        if self._local_server:
            self._local_server.close()
        self._shared_mem.detach()
        self.app.quit()

    # ─────────────────────────────────────────────────────────
    #  Event loop
    # ─────────────────────────────────────────────────────────
    def run(self) -> int:
        log.info("Wallpaper Sensei is running.  Tray icon active.")
        return self.app.exec()


# ═════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════
def main() -> None:
    ctrl = WallpaperSenseiApp()
    sys.exit(ctrl.run())


if __name__ == "__main__":
    main()
