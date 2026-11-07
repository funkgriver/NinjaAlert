"""NinjaAlert - floating transparent notification overlays + audio alerts.

ToastManager owns the popup stack (corner-anchored, max N visible).
NotificationOverlay is a frameless, translucent, always-on-top, draggable
window: shows item icon, name, old -> new price, % jump, volume/context,
auto-fades after popup_seconds (hover pauses the countdown).
Click on the popup body opens the matching poe.ninja page; ✕ dismisses.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import (
    QObject,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from api_client import BASE_URL, FetchMeta, format_number, format_price
from categories import get_cat

log = logging.getLogger("ninjaalert.overlay")

TOAST_WIDTH = 360
TOAST_HEIGHT = 100
TOAST_GAP = 10

ACCENT_UP = "#41d98c"     # price spike
ACCENT_VOL = "#e8b93e"    # listing-volume surge
ACCENT_MUTE = "#9aa4b2"

try:
    import winsound  # noqa: WPS433 (Windows only)
except ImportError:  # non-Windows dev/test machines
    winsound = None  # type: ignore[assignment]


# --------------------------------------------------------------------- sound
def play_alert_sound(enabled: bool, base_dir: str) -> None:
    """Subtle two-tone cue. alert.wav (next to the exe) overrides the beep."""
    if not enabled:
        return
    wav = os.path.join(base_dir, "alert.wav")
    if winsound is not None and os.path.isfile(wav):
        try:
            winsound.PlaySound(
                wav,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return
        except Exception as exc:
            log.warning("could not play alert.wav: %s", exc)
    threading.Thread(target=_beep_two_tone, daemon=True).start()


def _beep_two_tone() -> None:
    try:
        if winsound is not None:
            winsound.Beep(880, 140)     # gentle rise: 880 Hz -> 1245 Hz
            winsound.Beep(1245, 190)
        else:
            QGuiApplication.beep()
    except Exception as exc:
        log.debug("beep failed: %s", exc)


# ---------------------------------------------------------------- icon cache
ICON_CACHE_MAX = 512          # LRU cap: alerted-item icons must not accumulate


class IconLoader(QObject):
    """Async icon downloader with a bounded in-memory cache (no disk writes)."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._cache: "OrderedDict[str, Optional[QPixmap]]" = OrderedDict()
        self._pending: Dict[str, List[Callable]] = {}

    def get(self, url: str, callback: Callable[[Optional[QPixmap]], None]) -> None:
        if url in self._cache:
            self._cache.move_to_end(url)       # LRU touch
            callback(self._cache[url])
            return
        self._pending.setdefault(url, []).append(callback)
        if len(self._pending.get(url, [])) > 1:
            return  # download already in flight
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirects)
        reply = self._nam.get(req)
        reply.finished.connect(lambda: self._on_finished(url, reply))

    def _on_finished(self, url: str, reply: QNetworkReply) -> None:
        pix: Optional[QPixmap] = None
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                if data:
                    pix = QPixmap()
                    if not pix.loadFromData(data):
                        pix = None
        except Exception as exc:
            log.debug("icon load failed for %s: %s", url, exc)
        finally:
            reply.deleteLater()
        self._cache[url] = pix
        self._evict()
        for cb in self._pending.pop(url, []):
            try:
                cb(pix)
            except Exception:
                log.exception("icon callback failed")

    def _evict(self) -> None:
        while len(self._cache) > ICON_CACHE_MAX:
            self._cache.popitem(last=False)    # evict least-recently-used


# --------------------------------------------------------------- one overlay
class NotificationOverlay(QWidget):
    closed = pyqtSignal(object)

    def __init__(
        self,
        alert,
        meta: FetchMeta,
        popup_seconds: float,
        icon_loader: IconLoader,
        site_url: Optional[str],
        price_text: Optional[str] = None,
        meta_text: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._price_override = price_text
        self._meta_override = meta_text
        self._alert = alert
        self._site_url = site_url
        self._drag_offset: Optional[QPoint] = None
        self._dragging = False
        self._user_moved = False
        self._remaining = float(popup_seconds)
        self._total = float(popup_seconds)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(TOAST_WIDTH, TOAST_HEIGHT)
        self.setMouseTracking(True)

        # green accent for price spikes, amber for listing-volume surges
        accent = ACCENT_UP if alert.old_price != alert.new_price else ACCENT_VOL

        card = QFrame(self)
        card.setObjectName("toastCard")
        card.setGeometry(0, 0, TOAST_WIDTH, TOAST_HEIGHT)
        card.setStyleSheet(
            "#toastCard {"
            " background: rgba(17, 20, 28, 243);"
            " border: 1px solid rgba(255, 255, 255, 28);"
            " border-radius: 10px; }"
        )
        root = QVBoxLayout(card)
        root.setContentsMargins(12, 10, 8, 4)
        root.setSpacing(2)

        content = QHBoxLayout()
        content.setSpacing(10)

        self._icon_label = QLabel()
        self._icon_label.setFixedSize(40, 40)
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setStyleSheet(
            "background: rgba(255,255,255,14); border-radius: 6px; color: #9aa4b2;"
        )
        self._icon_label.setText("◈")
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if alert.icon:
            icon_loader.get(alert.icon, self._set_icon)
        content.addWidget(self._icon_label)

        mid = QVBoxLayout()
        mid.setSpacing(1)

        title_font = QFont()
        title_font.setPointSizeF(10.0)
        title_font.setBold(True)
        self._title = QLabel()
        self._title.setFont(title_font)
        self._title.setStyleSheet("color: #f2f4f8;")
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(self._title)

        self._price_label = QLabel()
        self._price_label.setStyleSheet(f"color: {accent}; font-size: 13px; font-weight: 600;")
        self._price_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(self._price_label)

        self._meta_label = QLabel()
        self._meta_label.setStyleSheet("color: #9aa4b2; font-size: 10px;")
        self._meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(self._meta_label)
        content.addLayout(mid, stretch=1)

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QToolButton { color: #9aa4b2; background: transparent; border: none;"
            " font-size: 12px; padding: 2px 4px; }"
            "QToolButton:hover { color: #ffffff; }"
        )
        close_btn.clicked.connect(self.dismiss)
        content.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(content, stretch=1)

        self._progress = QFrame(card)
        self._progress.setFixedHeight(3)
        self._progress.setStyleSheet(
            f"background: {accent}; border-radius: 1px; border: none;"
        )
        root.addWidget(self._progress)

        self._fill_text(alert, meta)
        if alert.icon is None:
            self._icon_label.setText("◈")

        # countdown bar + auto close
        self._tick = QTimer(self)
        self._tick.setInterval(60)
        self._tick.timeout.connect(self._on_tick)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(int(popup_seconds * 1000))
        self._hold.timeout.connect(self._start_fadeout)

        self._fade_in = QPropertyAnimation(self, b"windowOpacity")
        self._fade_in.setDuration(150)
        self._fade_out = QPropertyAnimation(self, b"windowOpacity")
        self._fade_out.setDuration(280)
        self._fade_out.finished.connect(self._finalize_close)

    # ------------------------------------------------------------- content
    def _set_icon(self, pix: Optional[QPixmap]) -> None:
        if pix is not None and not pix.isNull():
            self._icon_label.setPixmap(
                pix.scaled(36, 36,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            )
            self._icon_label.setText("")

    def _fill_text(self, alert, meta: FetchMeta) -> None:
        fm = self._title.fontMetrics()
        self._title.setText(
            fm.elidedText(alert.name, Qt.TextElideMode.ElideRight, TOAST_WIDTH - 130)
        )

        if self._price_override is not None:
            self._price_label.setText(self._price_override)
        else:
            pct_text = f"{alert.pct:+.1f}%" if alert.old_price != alert.new_price else ""
            arrow = " → " if pct_text else ""
            self._price_label.setText(
                f"{format_price(alert.old_price, meta)}{arrow}"
                f"{format_price(alert.new_price, meta)}   {pct_text}"
            )

        if self._meta_override is not None:
            self._meta_label.setText(self._meta_override)
            return

        parts: List[str] = [get_cat(alert.game, alert.category).label]
        if getattr(alert, "label", ""):
            parts.append(str(alert.label))
        if getattr(alert, "phase", ""):
            parts.append(str(alert.phase))
        if getattr(alert, "z_adj", 0.0):
            parts.append(f"z {alert.z_adj:+.1f}")
        if alert.spark_total_change is not None:
            parts.append(f"7d {alert.spark_total_change:+.1f}%")
        if alert.vol_ratio is not None:
            parts.append(f"RVOL ×{alert.vol_ratio:.1f}")
        elif alert.list_count is not None:
            parts.append(f"listings {format_number(alert.list_count)}")
        parts.append(alert.league)
        self._meta_label.setText("  ·  ".join(parts))

    # ---------------------------------------------------------------- show
    def show_at(self, pos: QPoint, opacity: float) -> None:
        self.move(pos)
        self.setWindowOpacity(0.0)
        self.show()
        self._fade_in.stop()
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(float(opacity))
        self._fade_in.start()
        self._hold.start()
        self._tick.start()
        self._update_progress()

    # ------------------------------------------------------------ behaviors
    def _on_tick(self) -> None:
        if self._hold.isActive():
            self._remaining = max(0.0, self._remaining - self._tick.interval() / 1000.0)
        self._update_progress()

    def _update_progress(self) -> None:
        frac = self._remaining / self._total if self._total > 0 else 0.0
        width = int((TOAST_WIDTH - 22) * max(0.0, min(1.0, frac)))
        self._progress.setFixedWidth(max(width, 2))

    def _start_fadeout(self) -> None:
        self._tick.stop()
        self._fade_out.stop()
        self._fade_out.setStartValue(self.windowOpacity())
        self._fade_out.setEndValue(0.0)
        self._fade_out.start()

    def _finalize_close(self) -> None:
        self._tick.stop()
        self.hide()
        self.closed.emit(self)
        self.deleteLater()

    def dismiss(self) -> None:
        self._hold.stop()
        self._tick.stop()
        self._fade_out.stop()
        self._fade_in.stop()
        self._finalize_close()

    # hover pauses the auto-dismiss countdown
    def enterEvent(self, event) -> None:
        self._hold.stop()

    def leaveEvent(self, event) -> None:
        if not self._dragging:
            self._remaining = self._total
            self._hold.start()

    # drag anywhere on the popup
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if not self._dragging and (new_pos - self.pos()).manhattanLength() > 6:
                self._dragging = True
                self._user_moved = True
            if self._dragging:
                self.move(new_pos)

    def mouseReleaseEvent(self, event) -> None:
        was_click = self._drag_offset is not None and not self._dragging
        self._drag_offset = None
        self._dragging = False
        if was_click and self._site_url:
            try:
                QGuiApplication.openUrl(QUrl(self._site_url))
            except Exception:
                log.exception("could not open %s", self._site_url)
            self.dismiss()

    @property
    def user_moved(self) -> bool:
        return self._user_moved


# ------------------------------------------------------------------ manager
class ToastManager(QObject):
    """Shows alert popups, keeps them stacked in the configured corner."""

    def __init__(self, config_getter: Callable[[], object], base_dir: str,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cfg_getter = config_getter
        self._base_dir = base_dir
        self._active: List[NotificationOverlay] = []
        self._icons = IconLoader(self)

    # main loop pushes alerts here (queued connection from poller side)
    def show_alert(self, alert, meta: FetchMeta, sound: bool = True) -> None:
        cfg = self._cfg_getter()
        if sound:
            play_alert_sound(cfg.alerts.sound, self._base_dir)

        limit = int(cfg.alerts.max_popups)
        while len(self._active) >= limit and self._active:
            oldest = self._active.pop(0)
            oldest.dismiss()

        overlay = NotificationOverlay(
            alert=alert,
            meta=meta,
            popup_seconds=float(cfg.alerts.popup_seconds),
            icon_loader=self._icons,
            site_url=self._site_url_for(alert, meta),
        )
        overlay.closed.connect(self._on_closed)
        self._active.append(overlay)
        self._position(overlay, len(self._active) - 1)
        overlay.show_at(self._slot_pos(len(self._active) - 1), float(cfg.overlay.opacity))

    def show_digest(self, digest, game: str, league: str) -> None:
        """Amber summary line for anomalies folded by the alert budget."""
        try:
            from analyzer import Alert
            from api_client import FetchMeta

            cfg = self._cfg_getter()
            cats = " · ".join(f"{cat.split(':', 2)[-1]} ×{n}"
                              for cat, n in digest.categories[:3])
            names = ", ".join(digest.names[: digest.count and len(digest.names)])
            meta = FetchMeta(game=game, league=league, category="digest",
                             source="exchange", primary="chaos")
            alert = Alert(game=game, league=league, category="digest",
                          name=f"➕ {digest.count} more moves",
                          old_price=0.0, new_price=0.0, pct=0.0,
                          list_count=None, vol_ratio=None, icon=None,
                          details_id=None, source="exchange")
            limit = int(cfg.alerts.max_popups)
            while len(self._active) >= limit and self._active:
                oldest = self._active.pop(0)
                oldest.dismiss()
            overlay = NotificationOverlay(
                alert=alert, meta=meta,
                popup_seconds=float(cfg.alerts.popup_seconds),
                icon_loader=self._icons, site_url=None,
                price_text=cats or "alert budget reached",
                meta_text=f"{names} — folded to keep the feed clean",
            )
            overlay.closed.connect(self._on_closed)
            self._active.append(overlay)
            self._position(overlay, len(self._active) - 1)
            overlay.show_at(self._slot_pos(len(self._active) - 1), float(cfg.overlay.opacity))
        except Exception:
            log.exception("digest toast failed")

    def _on_closed(self, overlay) -> None:
        if overlay in self._active:
            self._active.remove(overlay)
        self._relayout()

    def dismiss_all(self) -> None:
        for overlay in list(self._active):
            overlay.dismiss()
        self._active.clear()

    # ------------------------------------------------------------ placement
    def _slot_pos(self, index: int) -> QPoint:
        cfg = self._cfg_getter()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        margin = int(cfg.overlay.margin)
        corner = cfg.overlay.corner
        step = TOAST_HEIGHT + TOAST_GAP
        if corner == "bottom-right":
            return QPoint(geo.right() - margin - TOAST_WIDTH,
                          geo.bottom() - margin - TOAST_HEIGHT - index * step)
        if corner == "bottom-left":
            return QPoint(geo.left() + margin,
                          geo.bottom() - margin - TOAST_HEIGHT - index * step)
        if corner == "top-right":
            return QPoint(geo.right() - margin - TOAST_WIDTH,
                          geo.top() + margin + index * step)
        return QPoint(geo.left() + margin, geo.top() + margin + index * step)  # top-left

    def _position(self, overlay: NotificationOverlay, index: int) -> None:
        if not overlay.user_moved:
            overlay.move(self._slot_pos(index))

    def _relayout(self) -> None:
        for i, overlay in enumerate(self._active):
            self._position(overlay, i)

    # --------------------------------------------------------------- urls
    @staticmethod
    def _site_url_for(alert, meta: FetchMeta) -> Optional[str]:
        """Best-effort poe.ninja page for the clicked popup."""
        game = alert.game
        league_slug = _league_slug(meta.league)
        cat = get_cat(game, alert.category)
        if league_slug and cat.slug:
            base = f"{BASE_URL}/{game}/economy/{league_slug}/{cat.slug}"
            if alert.details_id and cat.source == "item":
                return f"{base}/{alert.details_id}"
            return base
        return f"{BASE_URL}/{game}/economy"


def _league_slug(league: str) -> str:
    """'HC Forbidden Rites' -> 'forbiddenriteshc'; 'Allflame' -> 'allflame'."""
    name = league.strip()
    hardcore = False
    for prefix in ("Hardcore ", "HC "):
        if name.startswith(prefix):
            hardcore = True
            name = name[len(prefix):]
    slug = name.lower().replace(" ", "").replace("'", "")
    return slug + ("hc" if hardcore else "")
