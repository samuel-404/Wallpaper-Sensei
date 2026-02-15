"""
Wallpaper Sensi — Discover Tab
===============================
Scrollable grid gallery of wallpaper cards fetched live from Supabase.

Each card shows a thumbnail (rounded 12 px corners), title, author, and
either an *Apply* button (if the wallpaper is already local) or a
*Download* button.  Downloads stream via httpx with progress feedback.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
import logging
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QUrl
from PyQt6.QtGui import (
    QPixmap, QPainter, QPainterPath, QColor, QFont, QCursor,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QScrollArea,
    QLabel, QPushButton, QFrame, QGraphicsDropShadowEffect,
    QProgressBar,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

import httpx

log = logging.getLogger("WallpaperSensei.Discover")

# ── Supabase connection details ─────────────────────────────
SUPABASE_URL = "https://bomrbqwrwnzdnidxbkjt.supabase.co"
SUPABASE_KEY = (
    "sb_publishable_Ca3qt3Gnc58O7vCM0nNVyg_-l8I-Dfv"
)

# ── Local wallpapers directory ──────────────────────────────
WALLPAPERS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wallpapers"
)


# ═════════════════════════════════════════════════════════════
#  Background worker — fetches wallpaper list from Supabase
# ═════════════════════════════════════════════════════════════
class _FetchWorker(QThread):
    """Calls the Supabase PostgREST endpoint via httpx."""

    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def run(self) -> None:
        try:
            url = f"{SUPABASE_URL}/rest/v1/wallpapers?select=*"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            }
            resp = httpx.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            self.finished.emit(resp.json())
        except Exception as exc:
            self.error.emit(str(exc))


# ═════════════════════════════════════════════════════════════
#  Background worker — downloads a single wallpaper file
# ═════════════════════════════════════════════════════════════
class _DownloadWorker(QThread):
    """
    Streams a file from *url* via httpx into a temporary directory,
    emitting *progress* (0-100) as it goes.
    On completion *finished* carries the local temp file path.
    """

    progress = pyqtSignal(int)          # 0-100
    finished = pyqtSignal(str, dict)    # (temp_file_path, original_data)
    error    = pyqtSignal(str, dict)    # (error_message, original_data)

    def __init__(self, url: str, data: dict, parent=None) -> None:
        super().__init__(parent)
        self._url = url
        self._data = data

    def run(self) -> None:
        try:
            with httpx.stream("GET", self._url, timeout=120,
                              follow_redirects=True) as resp:
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                # Determine a safe filename from the URL
                fname = self._url.rsplit("/", 1)[-1].split("?")[0] or "download"
                tmp_dir = tempfile.mkdtemp(prefix="wps_")
                tmp_path = os.path.join(tmp_dir, fname)

                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65_536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            self.progress.emit(
                                min(100, int(downloaded * 100 / total))
                            )

            self.progress.emit(100)
            self.finished.emit(tmp_path, self._data)
        except Exception as exc:
            self.error.emit(str(exc), self._data)


# ═════════════════════════════════════════════════════════════
#  WallpaperCard — single gallery card
# ═════════════════════════════════════════════════════════════
class WallpaperCard(QFrame):
    """
    Displays a wallpaper thumbnail, title, author, and an action
    button (Download / Apply).
    """

    apply_clicked    = pyqtSignal(dict)
    download_clicked = pyqtSignal(dict)

    CARD_W, CARD_H = 220, 290
    THUMB_W, THUMB_H = 200, 130

    def __init__(self, data: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data = data
        self._build_card()
        self._apply_frame_style()

    # ── layout ──────────────────────────────────────────────
    def _build_card(self) -> None:
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # ── thumbnail placeholder ───────────────────────────
        self._thumb = QLabel()
        self._thumb.setFixedSize(self.THUMB_W, self.THUMB_H)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setStyleSheet(
            "background: #1e1e24; border-radius: 12px; color: #555;"
        )
        self._thumb.setText("⏳")
        lay.addWidget(self._thumb)

        # ── title ───────────────────────────────────────────
        title = QLabel(self._data.get("title", "Untitled"))
        title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #e0e0e0; background: transparent;")
        title.setWordWrap(True)
        title.setMaximumHeight(36)
        lay.addWidget(title)

        # ── author ──────────────────────────────────────────
        author = QLabel(f"by {self._data.get('author', 'Unknown')}")
        author.setFont(QFont("Segoe UI", 9))
        author.setStyleSheet("color: #888; background: transparent;")
        lay.addWidget(author)

        lay.addStretch()

        # ── progress bar (hidden by default) ────────────────
        self._progress = QProgressBar()
        self._progress.setFixedHeight(6)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(_progress_css())
        self._progress.hide()
        lay.addWidget(self._progress)

        # ── status label ("Downloading…" etc.) ──────────────
        self._status_lbl = QLabel()
        self._status_lbl.setFont(QFont("Segoe UI", 8))
        self._status_lbl.setStyleSheet(
            "color: #6c9ddb; background: transparent;"
        )
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.hide()
        lay.addWidget(self._status_lbl)

        # ── action button ───────────────────────────────────
        slug = self._slug()
        is_local = os.path.isdir(os.path.join(WALLPAPERS_DIR, slug))

        self._btn = QPushButton("Apply" if is_local else "Download")
        self._btn.setFixedHeight(32)
        self._btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._is_local = is_local

        if is_local:
            self._set_apply_mode()
        else:
            self._set_download_mode()

        lay.addWidget(self._btn)

    # ── button mode helpers ─────────────────────────────────
    def _set_apply_mode(self) -> None:
        """Switch the button to 'Apply' state."""
        self._is_local = True
        self._btn.setText("Apply")
        self._btn.setEnabled(True)
        self._btn.setStyleSheet(_btn_css("#6c3adb", "#7c4aeb"))
        # Disconnect previous handlers and reconnect
        try:
            self._btn.clicked.disconnect()
        except TypeError:
            pass
        self._btn.clicked.connect(
            lambda: self.apply_clicked.emit(self._data)
        )

    def _set_download_mode(self) -> None:
        """Switch the button to 'Download' state."""
        self._btn.setText("Download")
        self._btn.setEnabled(True)
        self._btn.setStyleSheet(_btn_css("#2a7de1", "#3a8df1"))
        try:
            self._btn.clicked.disconnect()
        except TypeError:
            pass
        self._btn.clicked.connect(
            lambda: self.download_clicked.emit(self._data)
        )

    # ── progress feedback ───────────────────────────────────
    def set_downloading(self) -> None:
        """Show progress bar and disable button during download."""
        self._btn.setEnabled(False)
        self._btn.setText("Downloading…")
        self._btn.setStyleSheet(_btn_css("#444", "#444"))
        self._progress.setValue(0)
        self._progress.show()
        self._status_lbl.setText("Starting download…")
        self._status_lbl.show()

    def set_progress(self, pct: int) -> None:
        """Update the progress bar (0-100)."""
        self._progress.setValue(pct)
        self._status_lbl.setText(f"Downloading… {pct}%")

    def set_download_complete(self) -> None:
        """Hide progress, switch button to Apply."""
        self._progress.hide()
        self._status_lbl.setText("✓ Ready")
        self._status_lbl.setStyleSheet(
            "color: #4caf50; background: transparent;"
        )
        self._set_apply_mode()

    def set_download_error(self, msg: str) -> None:
        """Show error and re-enable Download button."""
        self._progress.hide()
        self._status_lbl.setText(f"✕ {msg[:40]}")
        self._status_lbl.setStyleSheet(
            "color: #e74c3c; background: transparent;"
        )
        self._set_download_mode()

    # ── frame styling + shadow ──────────────────────────────
    def _apply_frame_style(self) -> None:
        self.setObjectName("WallpaperCard")
        self.setStyleSheet("""
            #WallpaperCard {
                background: #1a1a1e;
                border: 1px solid #2a2a2e;
                border-radius: 14px;
            }
            #WallpaperCard:hover {
                border: 1px solid #6c3adb;
                background: #1e1e26;
            }
        """)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 80))
        self.setGraphicsEffect(shadow)

    # ── thumbnail from downloaded pixmap ────────────────────
    def set_thumbnail(self, pixmap: QPixmap) -> None:
        target = QSize(self.THUMB_W, self.THUMB_H)

        # Scale-to-fill then center-crop
        scaled = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (scaled.width()  - target.width())  // 2
        y = (scaled.height() - target.height()) // 2
        cropped = scaled.copy(x, y, target.width(), target.height())

        # Rounded-corner mask
        rounded = QPixmap(target)
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(
            0.0, 0.0,
            float(target.width()), float(target.height()),
            12.0, 12.0,
        )
        p.setClipPath(clip)
        p.drawPixmap(0, 0, cropped)
        p.end()

        self._thumb.setPixmap(rounded)
        self._thumb.setText("")

    # ── helpers ─────────────────────────────────────────────
    def _slug(self) -> str:
        return self._data.get(
            "slug",
            self._data.get("title", "").lower().replace(" ", "_"),
        )


# ═════════════════════════════════════════════════════════════
#  DiscoverTab — main widget
# ═════════════════════════════════════════════════════════════
class DiscoverTab(QWidget):
    """
    Full-page discover gallery.  Emits signals when the user clicks
    "Apply" or "Download" on a card.
    """

    wallpaper_apply    = pyqtSignal(dict)   # wallpaper already local
    wallpaper_download = pyqtSignal(dict)   # needs downloading first

    _COLS_MIN = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: list[WallpaperCard] = []
        self._card_map: dict[str, WallpaperCard] = {}  # slug → card
        self._net = QNetworkAccessManager(self)
        self._worker: Optional[_FetchWorker] = None
        self._dl_workers: list[_DownloadWorker] = []    # prevent GC
        self._build_ui()
        self._fetch_wallpapers()

    # ── UI scaffold ─────────────────────────────────────────
    def _build_ui(self) -> None:
        self.setStyleSheet("background: #121212;")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)

        # header
        hdr = QLabel("🔍  Discover Wallpapers")
        hdr.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        hdr.setStyleSheet(
            "color: #e0e0e0; padding-bottom: 10px; background: transparent;"
        )
        root.addWidget(hdr)

        # status label (shown while loading)
        self._status = QLabel("Loading wallpapers…")
        self._status.setStyleSheet(
            "color: #888; font-size: 13px; background: transparent;"
        )
        root.addWidget(self._status)

        # scrollable grid
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setStyleSheet(_scroll_css())

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(18)
        self._grid.setContentsMargins(0, 12, 0, 12)

        self._scroll.setWidget(self._grid_widget)
        root.addWidget(self._scroll)

    # ── Supabase fetch ──────────────────────────────────────
    def _fetch_wallpapers(self) -> None:
        self._worker = _FetchWorker()
        self._worker.finished.connect(self._on_data)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_data(self, rows: list) -> None:
        self._status.hide()

        if not rows:
            self._status.setText("No wallpapers found in the catalogue.")
            self._status.show()
            return

        cols = self._ideal_cols()

        for idx, wp in enumerate(rows):
            card = WallpaperCard(wp)
            card.apply_clicked.connect(self.wallpaper_apply.emit)
            card.download_clicked.connect(self._start_download)
            self._cards.append(card)

            slug = card._slug()
            self._card_map[slug] = card

            self._grid.addWidget(card, idx // cols, idx % cols)

            # async thumbnail download
            thumb_url = wp.get("thumbnail_url")
            if thumb_url:
                self._load_thumb(card, thumb_url)

        log.info("Loaded %d wallpapers from Supabase.", len(rows))

    # ── Download lifecycle ──────────────────────────────────
    def _start_download(self, data: dict) -> None:
        """Kick off a background download for the wallpaper."""
        slug = data.get(
            "slug", data.get("title", "").lower().replace(" ", "_")
        )
        card = self._card_map.get(slug)
        if card is None:
            return

        file_url = data.get("file_url")
        if not file_url:
            card.set_download_error("No file URL")
            return

        card.set_downloading()

        worker = _DownloadWorker(file_url, data, parent=self)
        worker.progress.connect(card.set_progress)
        worker.finished.connect(
            lambda path, d: self._on_download_done(path, d, card)
        )
        worker.error.connect(
            lambda msg, d: card.set_download_error(msg)
        )
        self._dl_workers.append(worker)  # prevent GC
        worker.start()
        log.info("Download started: %s", file_url)

    def _on_download_done(
        self, tmp_path: str, data: dict, card: WallpaperCard
    ) -> None:
        """Handle a completed download: extract / move / generate wrapper."""
        slug = data.get(
            "slug", data.get("title", "").lower().replace(" ", "_")
        )
        dest_dir = os.path.join(WALLPAPERS_DIR, slug)
        os.makedirs(dest_dir, exist_ok=True)

        try:
            lower = tmp_path.lower()

            if lower.endswith(".zip"):
                # ── ZIP: extract into wallpapers/<slug>/ ─────────
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(dest_dir)
                log.info("Extracted ZIP to %s", dest_dir)

            elif lower.endswith(".mp4"):
                # ── MP4: move + generate looping HTML wrapper ────
                video_name = os.path.basename(tmp_path)
                final_video = os.path.join(dest_dir, video_name)
                shutil.move(tmp_path, final_video)
                _generate_video_html(dest_dir, video_name)
                log.info("MP4 wallpaper created at %s", dest_dir)

            else:
                # ── Other file: just move it into the folder ─────
                shutil.move(tmp_path, os.path.join(
                    dest_dir, os.path.basename(tmp_path)
                ))
                log.info("File moved to %s", dest_dir)

            card.set_download_complete()
            # Emit apply so main.py can load it immediately
            self.wallpaper_apply.emit(data)

        except Exception as exc:
            card.set_download_error(str(exc))
            log.error("Post-download processing failed: %s", exc)
        finally:
            # Clean up temp directory
            tmp_dir = os.path.dirname(tmp_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _on_error(self, msg: str) -> None:
        self._status.setText(f"⚠  {msg}")
        self._status.setStyleSheet(
            "color: #e74c3c; font-size: 13px; background: transparent;"
        )
        log.error("Supabase fetch error: %s", msg)

    # ── thumbnail download ──────────────────────────────────
    def _load_thumb(self, card: WallpaperCard, url: str) -> None:
        reply = self._net.get(QNetworkRequest(QUrl(url)))
        # prevent GC and capture card reference
        reply.finished.connect(lambda r=reply, c=card: self._on_thumb(r, c))

    def _on_thumb(self, reply: QNetworkReply, card: WallpaperCard) -> None:
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pix = QPixmap()
            pix.loadFromData(reply.readAll().data())
            if not pix.isNull():
                card.set_thumbnail(pix)
        reply.deleteLater()

    # ── responsive grid reflow ──────────────────────────────
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        if not self._cards:
            return
        cols = self._ideal_cols()
        for i, card in enumerate(self._cards):
            self._grid.removeWidget(card)
            self._grid.addWidget(card, i // cols, i % cols)

    def _ideal_cols(self) -> int:
        return max(self._COLS_MIN, self.width() // 250)


# ─────────────────────────────────────────────────────────────
#  Stylesheet helpers (keep the main classes readable)
# ─────────────────────────────────────────────────────────────
def _btn_css(bg: str, hover: str) -> str:
    return f"""
        QPushButton {{
            background: {bg}; color: #fff; border: none;
            border-radius: 8px; font-weight: bold; font-size: 12px;
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:pressed {{ background: {bg}; }}
    """


def _scroll_css() -> str:
    return """
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical {
            background: #1a1a1a; width: 8px; border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #3a3a3a; min-height: 30px; border-radius: 4px;
        }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical { height: 0; }
    """


def _progress_css() -> str:
    return """
        QProgressBar {
            background: #2a2a2e; border: none; border-radius: 3px;
        }
        QProgressBar::chunk {
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #6c3adb, stop:1 #2a7de1
            );
            border-radius: 3px;
        }
    """


def _generate_video_html(dest_dir: str, video_name: str) -> None:
    """Create a minimal index.html that loops an mp4 as a wallpaper."""
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
    with open(os.path.join(dest_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

