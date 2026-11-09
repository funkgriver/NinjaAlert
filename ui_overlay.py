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
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
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
DIGEST_ROW_H = 34
DIGEST_MAX_BODY_H = 264       # expanded list viewport cap

ACCENT_UP = "#41d98c"     # price spike
ACCENT_VOL = "#e8b93e"    # listing-volume surge
ACCENT_MUTE = "#9aa4b2"
ACCENT_DOWN = "#e5734b"   # price drop

FRIENDLY_STATUS = {
    "SUSPECT": "⚠ Manipulation likely",
    "AMBIGUOUS": "🤔 Worth a look",
    "ORGANIC": "✅ Organic move",
}

VERDICT_GLYPH = {
    "SUSPECT": "⚠",
    "AMBIGUOUS": "🤔",
    "ORGANIC": "✅",
}


def friendly_status(label: str) -> str:
    """Human-readable 'should I care?' verdict for an alert label."""
    label = str(label or "")
    if label.startswith("SCENARIO"):
        return "🎯 Your scenario"
    return FRIENDLY_STATUS.get(label, label)

try:
    import winsound  # noqa: WPS433 (Windows only)
except ImportError:  # non-Windows dev/test machines
    winsound = None  # type: ignore[assignment]


# --------------------------------------------------------------------- sound
def play_alert_sound(enabled: bool, base_dir: str) -> None:
    """Alert cue: a custom alert.wav next to the exe wins, then the bundled
    PoE hitmarker default (assets/alert.wav), then the two-tone beep."""
    if not enabled:
        return
    if winsound is not None:
        from appconfig import resource_path
        override = os.path.join(base_dir, "alert.wav")
        for path in (override if os.path.isfile(override) else None,
                     resource_path("assets/alert.wav")):
            if not path:
                continue
            try:
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return
            except Exception as exc:
                log.warning("could not play %s: %s", path, exc)
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


def _icon_url_fallback(url: str) -> Optional[str]:
    """poe.ninja's /gen/image/<base64>/... URLs 404 from plain clients; the
    base64 payload carries the GGG art path, so build the classic
    web.poecdn.com/image/Art/<path>.png URL from it (verified 2026-11)."""
    if "/gen/image/" not in url:
        return None
    try:
        b64 = url.split("/gen/image/", 1)[1].split("/", 1)[0]
        import base64 as _b64
        data = _b64.b64decode(b64 + "=" * (-len(b64) % 4))
        import json as _json
        payload = _json.loads(data.decode("utf-8"))
        art = payload[-1].get("f") if isinstance(payload, list) else None
        if art:
            return f"https://web.poecdn.com/image/Art/{art}.png"
    except Exception:
        return None
    return None


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
        # API gen/image URLs 404 -> try the decoded poecdn art path first,
        # then the original URL as a fallback (either may start working)
        req_url = _icon_url_fallback(url) or url
        req = QNetworkRequest(QUrl(req_url))
        try:    # cosmetic nicety - a Qt enum drift must never kill dispatch
            req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        except Exception:
            log.debug("redirect policy attribute not supported")
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
    muted = pyqtSignal(str, float)          # key, minutes (one-click snooze)

    def __init__(
        self,
        alert,
        meta: FetchMeta,
        popup_seconds: float,
        icon_loader: IconLoader,
        site_url: Optional[str],
        price_text: Optional[str] = None,
        meta_text: Optional[str] = None,
        price_display: str = "both",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._price_override = price_text
        self._meta_override = meta_text
        self._price_display = price_display
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
            try:
                icon_loader.get(alert.icon, self._set_icon)
            except Exception:      # icons are cosmetic, alerts are not
                log.exception("icon fetch setup failed for %s", alert.icon)
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

        self._mute_btn = mute_btn = QToolButton()
        mute_btn.setText("🔇")
        mute_btn.setToolTip("Mute this item for 30 minutes")
        mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mute_btn.setStyleSheet(
            "QToolButton { color: #9aa4b2; background: transparent; border: none;"
            " font-size: 12px; padding: 2px 4px; }"
            "QToolButton:hover { color: #ffffff; }"
        )
        mute_btn.clicked.connect(self._mute_clicked)
        content.addWidget(mute_btn, alignment=Qt.AlignmentFlag.AlignTop)

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
                f"{format_price(alert.old_price, meta, self._price_display)}{arrow}"
                f"{format_price(alert.new_price, meta, self._price_display)}   {pct_text}"
            )

        if self._meta_override is not None:
            self._meta_label.setText(self._meta_override)
            return

        from webhook import plain_reason
        # plain-English "why it fired" instead of z/RVOL jargon; numbers in tooltip
        reason = plain_reason(alert.pct, getattr(alert, "vol_ratio", None),
                              getattr(alert, "supply_ratio", None),
                              1 if alert.pct >= 0 else -1,
                              getattr(alert, "label", ""))
        parts: List[str] = [get_cat(alert.game, alert.category).label, reason, alert.league]
        tip = (f"{friendly_status(getattr(alert, 'label', ''))} | phase {alert.phase or '-'} | "
               f"z {alert.z_adj:+.1f} | RVOL x{(alert.vol_ratio or 1):.1f}")
        if alert.spark_total_change is not None:
            tip += f" · 7d {alert.spark_total_change:+.1f}%"
        if alert.list_count is not None:
            tip += f" · listings {format_number(alert.list_count)}"
        self.setToolTip(tip)
        fm_meta = self._meta_label.fontMetrics()
        self._meta_label.setText(fm_meta.elidedText(
            "  ·  ".join(parts), Qt.TextElideMode.ElideRight, TOAST_WIDTH - 124))

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
    def _mute_clicked(self) -> None:
        try:
            self.muted.emit(self._alert.key, 30.0)
        except Exception:
            log.exception("mute emit failed")
        self.dismiss()

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
                # Qt6 removed QGuiApplication.openUrl - use QDesktopServices
                QDesktopServices.openUrl(QUrl(self._site_url))
            except Exception:
                log.exception("could not open %s", self._site_url)
            self.dismiss()

    @property
    def user_moved(self) -> bool:
        return self._user_moved


# ------------------------------------------------------------- digest overlay
class _DigestRow(QFrame):
    """One clickable folded-move row: opens the poe.ninja page on click."""

    clicked = pyqtSignal(str)

    def __init__(self, url: Optional[str], accent: str, glyph: str,
                 name: str, price_text: str, tooltip: str) -> None:
        super().__init__()
        self._url = url
        self.setFixedHeight(DIGEST_ROW_H - 6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QFrame { background: rgba(255,255,255,10); border: none;"
            " border-radius: 6px; }"
            "QFrame:hover { background: rgba(255,255,255,22); }"
        )
        self.setToolTip(tooltip)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(8)
        glyph_lbl = QLabel(glyph)
        glyph_lbl.setStyleSheet(f"color: {accent}; font-size: 12px;")
        glyph_lbl.setFixedWidth(18)
        lay.addWidget(glyph_lbl)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet("color: #e8ebf2; font-size: 11px;")
        name_lbl.setToolTip(name)
        lay.addWidget(name_lbl, stretch=1)
        price_lbl = QLabel(price_text)
        price_lbl.setStyleSheet(f"color: {accent}; font-size: 11px; font-weight: 600;")
        lay.addWidget(price_lbl)

    def mousePressEvent(self, event) -> None:
        # keep the grab inside the row: the scroll viewport must not steal the
        # press, or the release (and the navigation) never reaches us
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._url:
            self.clicked.emit(self._url)


class DigestOverlay(QWidget):
    """Interactive digest: folded moves with human category labels; click the
    card to expand the capped item list, click a row to open poe.ninja.
    Expanding pins the toast (auto-dismiss resumes on collapse)."""

    closed = pyqtSignal(object)
    resized = pyqtSignal(object)

    def __init__(self, digest, game: str, league: str, popup_seconds: float,
                 price_display: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._digest = digest
        self._game = game
        self._league = league
        self._price_display = price_display
        self._expanded = False
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
        self.setFixedWidth(TOAST_WIDTH)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._card = QFrame(self)
        self._card.setObjectName("digestCard")
        self._card.setGeometry(0, 0, TOAST_WIDTH, 100)
        self._card.setStyleSheet(
            "#digestCard {"
            " background: rgba(17, 20, 28, 243);"
            " border: 1px solid rgba(255, 255, 255, 28);"
            " border-radius: 10px; }"
        )
        root = QVBoxLayout(self._card)
        root.setContentsMargins(12, 10, 8, 6)
        root.setSpacing(3)

        head = QHBoxLayout()
        title_font = QFont()
        title_font.setPointSizeF(10.0)
        title_font.setBold(True)
        self._title = QLabel(f"➕ {digest.count} more moves")
        self._title.setFont(title_font)
        self._title.setStyleSheet("color: #f2f4f8;")
        head.addWidget(self._title, stretch=1)
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QToolButton { color: #9aa4b2; background: transparent; border: none;"
            " font-size: 12px; padding: 2px 4px; }"
            "QToolButton:hover { color: #ffffff; }"
        )
        close_btn.clicked.connect(self.dismiss)
        head.addWidget(close_btn)
        root.addLayout(head)

        self._chips = QLabel()
        self._chips.setStyleSheet(f"color: {ACCENT_VOL}; font-size: 11px; font-weight: 600;")
        self._chips.setText(self._human_chips())
        self._chips.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        root.addWidget(self._chips)

        self._names = QLabel()
        self._names.setStyleSheet("color: #9aa4b2; font-size: 10px;")
        self._names.setText(self._names_preview())
        root.addWidget(self._names)

        # expandable body: one clickable row per folded move (capped payload)
        self._body = QScrollArea()
        self._body.setWidgetResizable(True)
        self._body.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QWidget > QWidget { background: transparent; }"
        )
        body_widget = QWidget()
        rows_lay = QVBoxLayout(body_widget)
        rows_lay.setContentsMargins(0, 2, 0, 2)
        rows_lay.setSpacing(3)
        for item in digest.items:
            url = self._item_url(item)
            if item.key.endswith("#market"):
                glyph, accent = "⇄", ACCENT_MUTE
            elif item.direction >= 0:
                glyph, accent = VERDICT_GLYPH.get(
                    str(item.label).split("·")[0].strip(), "•"), ACCENT_UP
            else:
                glyph, accent = VERDICT_GLYPH.get(
                    str(item.label).split("·")[0].strip(), "•"), ACCENT_DOWN
            if str(item.label).startswith("SCENARIO"):
                glyph = "🎯"
            row = _DigestRow(url, accent, glyph, item.name, self._row_price(item),
                             friendly_status(str(item.label)))
            row.clicked.connect(self._open)
            rows_lay.addWidget(row)
        rows_lay.addStretch(1)
        self._body.setWidget(body_widget)
        root.addWidget(self._body)
        self._body.setVisible(False)

        self._hint = QLabel("▾ details")
        self._hint.setStyleSheet(f"color: {ACCENT_MUTE}; font-size: 10px;")
        root.addWidget(self._hint)

        self._progress = QFrame(self._card)
        self._progress.setFixedHeight(3)
        self._progress.setStyleSheet(
            f"background: {ACCENT_VOL}; border-radius: 1px; border: none;"
        )
        root.addWidget(self._progress)

        self._collapse_h = self._card.sizeHint().height()
        self._names_h = self._names.sizeHint().height()
        self._relayout_self()

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
    def _human_chips(self) -> str:
        parts = []
        for cat, n in self._digest.categories[:3]:
            cat_id = str(cat).rsplit(":", 1)[-1]
            try:
                label = get_cat(self._game, cat_id).label
            except Exception:
                label = cat_id
            parts.append(f"{label} ×{n}")
        more = len(self._digest.categories) - 3
        if more > 0:
            parts.append(f"+{more}")
        return "  ·  ".join(parts) if parts else "alert budget reached"

    def _names_preview(self) -> str:
        meta_line = f"{self._game.upper()} · {self._league}"
        shown = [str(n) for n in self._digest.names[:2]]
        rest = self._digest.count - len(shown)
        if shown and rest > 0:
            shown.append(f"…and {rest} more")
        line = ", ".join(shown) if shown else meta_line
        fm = self._names.fontMetrics()
        return fm.elidedText(f"{line}   ({meta_line})",
                             Qt.TextElideMode.ElideRight, TOAST_WIDTH - 40)

    def _row_price(self, item) -> str:
        try:
            from api_client import price_meta
            meta = price_meta(self._game, self._league, category="digest")
            arrow = "↑" if item.direction >= 0 else "↓"
            pct = f"{item.pct:+.1f}%" if item.pct else ""
            return (f"{arrow} {format_price(item.prev_price, meta, self._price_display)}"
                    f" → {format_price(item.price, meta, self._price_display)}  {pct}")
        except Exception:
            return f"{item.pct:+.1f}%"

    def _item_url(self, item) -> Optional[str]:
        """Best-effort poe.ninja page for one folded move (market rows open
        the category page instead)."""
        try:
            game = str(item.key).split(":", 1)[0]
            league_slug = _league_slug(self._league)
            if str(item.key).endswith("#market"):
                cat_id = str(item.category).rsplit(":", 1)[-1]
                cat = get_cat(game, cat_id)
                if league_slug and cat.slug:
                    return f"{BASE_URL}/{game}/economy/{league_slug}/{cat.slug}"
                return f"{BASE_URL}/{game}/economy"
            parts = str(item.key).split(":")
            cat_id = parts[2] if len(parts) > 2 else ""
            details_id = parts[3] if len(parts) > 3 else ""
            cat = get_cat(game, cat_id)
            if league_slug and cat.slug:
                base = f"{BASE_URL}/{game}/economy/{league_slug}/{cat.slug}"
                if details_id and cat.source == "item":
                    return f"{base}/{details_id}"
                return base
            return f"{BASE_URL}/{game}/economy"
        except Exception:
            return f"{BASE_URL}/{self._game}/economy"

    # ------------------------------------------------------------- geometry
    def _relayout_self(self) -> None:
        if self._expanded:
            need = len(self._digest.items) * DIGEST_ROW_H + 30
            body_h = min(need, DIGEST_MAX_BODY_H)
            self._body.setFixedHeight(body_h)
            self._body.setVisible(True)
            self._names.setVisible(False)
            self._hint.setText("▴ collapse")
            height = self._collapse_h - self._names_h + body_h
        else:
            self._body.setVisible(False)
            self._names.setVisible(True)
            self._hint.setText("▾ details")
            height = self._collapse_h
        self._card.setFixedHeight(height)
        self._card.setGeometry(0, 0, TOAST_WIDTH, height)
        self.setFixedSize(TOAST_WIDTH, height)
        self.resized.emit(self)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._relayout_self()
        if self._expanded:
            self._hold.stop()          # expanded digest is pinned
        elif not self._dragging:
            self._remaining = self._total
            self._hold.start()

    def _rows_count(self) -> int:
        lay = self._body.widget().layout()
        return sum(1 for i in range(lay.count())
                   if isinstance(lay.itemAt(i).widget(), _DigestRow))

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
    def _open(self, url: str) -> None:
        try:
            QDesktopServices.openUrl(QUrl(url))
        except Exception:
            log.exception("could not open %s", url)

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

    def enterEvent(self, event) -> None:
        self._hold.stop()

    def leaveEvent(self, event) -> None:
        if not self._dragging and not self._expanded:
            self._remaining = self._total
            self._hold.start()

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
        if was_click:
            self.toggle()

    @property
    def user_moved(self) -> bool:
        return self._user_moved


# ------------------------------------------------------------------ manager
class ToastManager(QObject):
    """Shows alert popups, keeps them stacked in the configured corner."""

    item_muted = pyqtSignal(str, float)     # key, minutes (from a toast 🔇)

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
            price_display=getattr(cfg.alerts, "price_display", "both"),
        )
        overlay.closed.connect(self._on_closed)
        overlay.muted.connect(self._on_muted)
        self._active.append(overlay)
        self._position(overlay, len(self._active) - 1)
        overlay.show_at(self._slot_pos(len(self._active) - 1), float(cfg.overlay.opacity))

    def _on_muted(self, key: str, minutes: float) -> None:
        self.item_muted.emit(key, minutes)

    def show_digest(self, digest, game: str, league: str) -> None:
        """Interactive amber summary for anomalies folded by the alert budget:
        human category labels, click to expand the capped folded-move list,
        click a row to open its poe.ninja page."""
        try:
            cfg = self._cfg_getter()
            limit = int(cfg.alerts.max_popups)
            while len(self._active) >= limit and self._active:
                oldest = self._active.pop(0)
                oldest.dismiss()
            overlay = DigestOverlay(
                digest=digest, game=game, league=league,
                popup_seconds=float(cfg.alerts.popup_seconds),
                price_display=getattr(cfg.alerts, "price_display", "both"),
            )
            overlay.closed.connect(self._on_closed)
            overlay.resized.connect(lambda _o: self._relayout())
            self._active.append(overlay)
            self._position(overlay, len(self._active) - 1)
            overlay.show_at(self._slot_pos(len(self._active) - 1),
                            float(cfg.overlay.opacity))
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
    def _stack_offset(self, index: int) -> int:
        """Vertical space taken by the overlays below/above this slot:
        overlays have different heights (digest expands), so stack by real
        heights instead of a fixed step."""
        return sum(o.height() + TOAST_GAP for o in self._active[:index])

    def _slot_pos(self, index: int) -> QPoint:
        cfg = self._cfg_getter()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        margin = int(cfg.overlay.margin)
        corner = cfg.overlay.corner
        below = self._stack_offset(index)
        own_h = self._active[index].height() if index < len(self._active) else TOAST_HEIGHT
        if corner in ("bottom-right", "bottom-left"):
            # clamp: a tall expanded digest must not escape past the top edge
            y = max(geo.bottom() - margin - own_h - below, geo.top() + margin)
            x = geo.right() - margin - TOAST_WIDTH if corner == "bottom-right" else geo.left() + margin
            return QPoint(x, y)
        y = min(geo.top() + margin + below, geo.bottom() - margin - own_h)
        x = geo.right() - margin - TOAST_WIDTH if corner == "top-right" else geo.left() + margin
        return QPoint(x, y)

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
