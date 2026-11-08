"""ui_history.py - History tab: past alerts rendered as read-only replicas of
the push toasts, so nothing that fired is ever lost when a popup was missed.

Data source: the alerts.jsonl journal (governor-admitted alerts + scenario
hits, see main._dispatch_alert). The tab reads only the journal tail
(bounded), applies live filters and renders AlertHistoryCard widgets that
mirror the toast design: dark rounded card, item icon, accent price line,
meta line. Clicking a card opens the matching poe.ninja page.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from api_client import BASE_URL, FetchMeta, format_price
from categories import get_cat
from ui_overlay import IconLoader, TOAST_HEIGHT, _league_slug, friendly_status

log = logging.getLogger("ninjaalert.history")

HISTORY_LIMIT = 200
REFRESH_SECONDS = 5

ACCENT_UP = "#41d98c"
ACCENT_DOWN = "#e5534b"
ACCENT_FLAT = "#e8b93e"

CARD_QSS = """
#histCard { background: rgba(17, 20, 28, 243); border: 1px solid rgba(255, 255, 255, 28);
            border-radius: 10px; }
#histCard:hover { border: 1px solid rgba(65, 217, 140, 140); }
#histCard QLabel { background: transparent; border: none; }
#histIcon { background: rgba(255,255,255,14); border-radius: 6px; color: #9aa4b2; }
#histTitle { color: #f2f4f8; font-size: 12px; font-weight: 700; }
#histPrice { font-size: 13px; font-weight: 600; }
#histMeta { color: #9aa4b2; font-size: 10px; }
#histTime { color: #98a2b3; font-size: 10px; }
"""

VALID_LABEL_FILTERS = ("any", "SUSPECT", "AMBIGUOUS", "ORGANIC", "SCENARIO")


def build_site_url(rec: Dict[str, Any]) -> Optional[str]:
    """Best-effort poe.ninja page for a journal record."""
    game = str(rec.get("game") or "")
    if not game:
        return None
    league = str(rec.get("league") or "")
    # journal records carry the full cohort ("<game>:<source>:<type>");
    # get_cat expects the bare category id
    cat_id = str(rec.get("category") or "").rsplit(":", 1)[-1]
    try:
        cat = get_cat(game, cat_id)
    except Exception:
        return f"{BASE_URL}/{game}/economy"
    slug = _league_slug(league)
    if slug and cat.slug:
        base = f"{BASE_URL}/{game}/economy/{slug}/{cat.slug}"
        details = rec.get("details_id")
        if details and cat.source == "item":
            return f"{base}/{details}"
        return base
    return f"{BASE_URL}/{game}/economy"


def _fmt_time(ts: float) -> str:
    """Compact relative time ('5m ago', '2h ago'), absolute for older items."""
    delta = time.time() - float(ts or 0.0)
    if delta < 0:
        delta = 0.0
    if delta < 3600.0:
        return f"{max(1, int(delta // 60))}m ago"
    if delta < 86400.0:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%d %b %H:%M", time.localtime(ts))


class AlertHistoryCard(QFrame):
    """Read-only toast replica for one journal record: click opens the item's
    poe.ninja page; a confirmation badge shows what the move did next."""

    def __init__(self, rec: Dict[str, Any], icon_loader: IconLoader,
                 price_display: str = "both", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rec = rec
        self._url = build_site_url(rec)
        self.setObjectName("histCard")
        self.setStyleSheet(CARD_QSS)
        self.setFixedHeight(TOAST_HEIGHT)
        self.setMinimumWidth(340)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            f"{rec.get('name')} · {rec.get('label')} · priority {rec.get('priority')}\n"
            f"z {rec.get('z_adj')} · rvol {rec.get('rvol')} · {rec.get('key')}\n"
            f"click — open on poe.ninja")

        # the card frame IS this widget: the layout follows the list width
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 10, 8)
        root.setSpacing(2)

        content = QHBoxLayout()
        content.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setObjectName("histIcon")
        icon_lbl.setFixedSize(40, 40)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setText("◈")
        icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        icon_url = rec.get("icon")
        if icon_url:
            icon_loader.get(str(icon_url), lambda pix: self._set_icon(icon_lbl, pix))
        content.addWidget(icon_lbl)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        title = QLabel(str(rec.get("name") or "?"))
        title.setObjectName("histTitle")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(title)

        price = QLabel(self._price_text(rec, price_display))
        price.setObjectName("histPrice")
        price.setStyleSheet(f"color: {self._accent(rec)};")
        price.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(price)

        meta = QLabel(self._meta_text(rec))
        meta.setObjectName("histMeta")
        meta.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        mid.addWidget(meta)
        content.addLayout(mid, stretch=1)

        when = QLabel(_fmt_time(float(rec.get("ts") or time.time())) + "   ↗")
        when.setObjectName("histTime")
        when.setAlignment(Qt.AlignmentFlag.AlignTop)
        when.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        right = QVBoxLayout()
        right.setSpacing(1)
        right.addWidget(when)
        badge = self._confirm_badge(rec)
        if badge is not None:
            badge_lbl = QLabel(badge[0])
            badge_lbl.setStyleSheet(f"color: {badge[1]}; font-size: 10px; font-weight: 700;")
            badge_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            badge_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            right.addWidget(badge_lbl)
        content.addLayout(right)
        root.addLayout(content, stretch=1)

    # ---------------------------------------------------------------- parts
    @staticmethod
    def _confirm_badge(rec: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """(text, color) for the confirmation outcome attached by
        webhook.attach_confirms; None when no re-scan has graded this alert."""
        conf = rec.get("confirm")
        if not isinstance(conf, dict):
            return None
        share = conf.get("share")
        if conf.get("price") is None and share is None:
            return ("– gone", ACCENT_FLAT)
        if conf.get("held"):
            txt = "✓ held" + (f" {float(share):.0%}" if isinstance(share, (int, float)) else "")
            return (txt, ACCENT_UP)
        return ("✗ reverted", ACCENT_DOWN)

    @staticmethod
    def _accent(rec: Dict[str, Any]) -> str:
        pct = rec.get("pct")
        try:
            pct = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            pct = 0.0
        if pct > 0:
            return ACCENT_UP
        if pct < 0:
            return ACCENT_DOWN
        return ACCENT_FLAT

    @staticmethod
    def _price_text(rec: Dict[str, Any], display: str) -> str:
        try:
            old = float(rec.get("prev_price"))
            new = float(rec.get("price"))
        except (TypeError, ValueError):
            return str(rec.get("text") or "")
        try:
            pct = float(rec.get("pct"))
        except (TypeError, ValueError):
            pct = (new / old - 1.0) * 100.0 if old else 0.0
        game = str(rec.get("game") or "poe1")
        meta = FetchMeta(game=game, league=str(rec.get("league") or ""),
                         category=str(rec.get("category") or ""), source="exchange",
                         primary="chaos" if game == "poe1" else "divine")
        arrow = " → " if old != new else ""
        pct_text = f"  {pct:+.1f}%" if old != new else ""
        return (f"{format_price(old, meta, display)}{arrow}"
                f"{format_price(new, meta, display)}{pct_text}")

    @staticmethod
    def _meta_text(rec: Dict[str, Any]) -> str:
        parts: List[str] = []
        if rec.get("category"):
            parts.append(str(rec["category"]).rsplit(":", 1)[-1])
        if rec.get("label"):
            parts.append(friendly_status(rec["label"]))
        if rec.get("phase"):
            parts.append(str(rec["phase"]))
        if rec.get("z_adj"):
            try:
                parts.append(f"z {float(rec['z_adj']):+.1f}")
            except (TypeError, ValueError):
                pass
        if rec.get("rvol") is not None:
            try:
                parts.append(f"RVOL ×{float(rec['rvol']):.1f}")
            except (TypeError, ValueError):
                pass
        if rec.get("league"):
            parts.append(str(rec["league"]))
        return "  ·  ".join(parts)

    def _set_icon(self, label: QLabel, pix: Optional[QPixmap]) -> None:
        if pix is not None and not pix.isNull():
            label.setPixmap(pix.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
            label.setText("")

    # ---------------------------------------------------------------- events
    def mousePressEvent(self, event) -> None:
        # accept the press so THIS widget keeps the mouse grab: otherwise the
        # press propagates to the enclosing QListWidget/viewport and the
        # release never comes back to us (the click "does nothing")
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._url and event.button() == Qt.MouseButton.LeftButton:
            try:
                QGuiApplication.openUrl(self._url)
            except Exception:
                log.exception("could not open %s", self._url)


class HistoryTab(QWidget):
    """Journal-backed alert history with live filters and auto-refresh."""

    def __init__(self, price_display_fn: Callable[[], str],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._price_display_fn = price_display_fn
        self._provider: Optional[Callable[..., List[Dict[str, Any]]]] = None
        self._rev_fn: Optional[Callable[[], int]] = None
        self._records: List[Dict[str, Any]] = []
        self._signature: Optional[tuple] = None
        self._loader = IconLoader(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(8)

        bar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setClearButtonEnabled(True)
        self._search.setPlaceholderText("Filter by item name…")
        self._search.textChanged.connect(lambda _: self._rebuild())
        bar.addWidget(self._search, stretch=1)

        self._game = QComboBox()
        self._game.addItem("All games", None)
        self._game.addItem("PoE 1", "poe1")
        self._game.addItem("PoE 2", "poe2")
        self._game.currentIndexChanged.connect(lambda _: self._rebuild())
        bar.addWidget(self._game)

        self._label = QComboBox()
        for value in VALID_LABEL_FILTERS:
            self._label.addItem("Any label" if value == "any" else value.title(), value)
        self._label.currentIndexChanged.connect(lambda _: self._rebuild())
        bar.addWidget(self._label)

        self._refresh_btn = QPushButton("⟳ Refresh")
        self._refresh_btn.clicked.connect(lambda: self.refresh(force=True))
        bar.addWidget(self._refresh_btn)
        root.addLayout(bar)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        root.addWidget(self._list, stretch=1)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_SECONDS * 1000)
        self._timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------- wiring
    def set_provider(self, fn: Callable[..., List[Dict[str, Any]]],
                     rev_fn: Optional[Callable[[], int]] = None) -> None:
        self._provider = fn
        self._rev_fn = rev_fn

    def activate(self) -> None:
        self.refresh(force=True)
        self._timer.start()

    def deactivate(self) -> None:
        self._timer.stop()

    # ------------------------------------------------------------ refresh
    def refresh(self, force: bool = False) -> None:
        if self._provider is None:
            return
        try:
            recs = list(self._provider(HISTORY_LIMIT))
        except Exception:
            log.exception("history provider failed")
            return
        # revision counts journal appends/rotates: with a full window the list
        # length and last ts can stay put while confirm outcomes arrive; the
        # minute bucket keeps relative-time labels ("5m ago") ticking over
        rev = self._rev_fn() if self._rev_fn else 0
        signature = (len(recs),
                     recs[0].get("ts") if recs else None,
                     recs[-1].get("ts") if recs else None,
                     rev, int(time.time() // 60))
        if not force and signature == self._signature:
            return
        self._signature = signature
        self._records = recs
        self._rebuild()

    def _passes(self, rec: Dict[str, Any]) -> bool:
        needle = self._search.text().strip().lower()
        if needle and needle not in str(rec.get("name") or "").lower():
            return False
        game = self._game.currentData()
        if game and rec.get("game") != game:
            return False
        label_filter = self._label.currentData()
        if label_filter and label_filter != "any":
            label = str(rec.get("label") or "")
            if label_filter == "SCENARIO":
                if not label.startswith("SCENARIO"):
                    return False
            elif label != label_filter:
                return False
        return True

    def _rebuild(self) -> None:
        self._list.clear()
        display = self._price_display_fn()
        shown = 0
        for rec in reversed(self._records):            # newest first
            if not self._passes(rec):
                continue
            card = AlertHistoryCard(rec, self._loader, display)
            item = QListWidgetItem(self._list)
            item.setSizeHint(QSize(0, card.height() + 2))
            self._list.addItem(item)
            self._list.setItemWidget(item, card)
            shown += 1
        if shown == 0:
            empty = QListWidgetItem(
                "no alerts match — they land here the moment they fire"
                if self._records else
                "no alerts yet — the journal fills as alerts fire")
            empty.setForeground(Qt.GlobalColor.gray)
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list.addItem(empty)
