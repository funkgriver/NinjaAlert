"""NinjaAlert - compact settings / control panel.

Sections: status header, PoE 1 / PoE 2 game toggle, league selector
(auto-detect from poe.ninja), watched categories per game, thresholds,
alert behavior. The window closes to tray (hide), it never quits the app.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from api_client import BASE_URL
from appconfig import Config
from categories import CATEGORIES

log = logging.getLogger("ninjaalert.settings")

STATE_COLORS = {
    "starting": "#e8b93e",
    "fetching": "#41d98c",
    "waiting": "#41d98c",
    "paused": "#9aa4b2",
    "disabled": "#6b7280",
    "error": "#e5534b",
}
STATE_TEXT = {
    "starting": "Starting…",
    "fetching": "Monitoring",
    "waiting": "Monitoring",
    "paused": "Paused",
    "disabled": "Game disabled",
    "error": "Error",
}

DARK_QSS = """
QWidget { background: #14171f; color: #e6e8ee; font-size: 12px; }
QLabel#titleLabel { font-size: 15px; font-weight: 700; color: #ffffff; }
QGroupBox {
    border: 1px solid #2a3140; border-radius: 8px; margin-top: 10px;
    font-weight: 600; color: #c9cfda;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background: #1b2029; border: 1px solid #2a3140; border-radius: 5px;
    padding: 4px 6px; color: #e6e8ee;
}
QComboBox QAbstractItemView {
    background: #1b2029; border: 1px solid #2a3140;
    selection-background-color: #2f6d4f;
}
QListWidget {
    background: #171b24; border: 1px solid #2a3140; border-radius: 6px;
}
QListWidget::item { padding: 2px; }
QListWidget::item:selected { background: #2f6d4f; }
QPushButton {
    background: #232a36; border: 1px solid #313b4d; border-radius: 6px;
    padding: 6px 12px; color: #e6e8ee;
}
QPushButton:hover { background: #2b3444; }
QPushButton:disabled { color: #6b7280; }
QPushButton#gameButton { padding: 7px 14px; font-weight: 600; }
QPushButton#gameButton:checked { background: #2f6d4f; border-color: #41d98c; color: #ffffff; }
QPushButton#primaryButton { background: #2f6d4f; border-color: #41d98c; font-weight: 600; }
QCheckBox::indicator { width: 15px; height: 15px; }
"""


def _status_dot_pixmap(color: str, d: int = 10) -> QPixmap:
    pix = QPixmap(d, d)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, d - 2, d - 2)
    p.end()
    return pix


class SettingsWindow(QWidget):
    settings_saved = pyqtSignal(object)       # new Config
    check_now_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    refresh_leagues_requested = pyqtSignal(str)

    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._editing_game = "poe1"
        self._leagues: Dict[str, List[str]] = {}
        self._checks: Dict[str, Dict[str, bool]] = {"poe1": {}, "poe2": {}}
        self._state = "starting"
        self._paused = False

        self.setWindowTitle("NinjaAlert — poe.ninja spike monitor")
        self.setMinimumWidth(470)
        self.setStyleSheet(DARK_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ---- header ------------------------------------------------------
        header = QHBoxLayout()
        title = QLabel("⚡ NinjaAlert")
        title.setObjectName("titleLabel")
        header.addWidget(title)
        header.addStretch(1)
        self._status_dot = QLabel()
        self._status_text = QLabel("Starting…")
        self._status_text.setStyleSheet("color: #9aa4b2;")
        header.addWidget(self._status_dot)
        header.addWidget(self._status_text)
        root.addLayout(header)

        # ---- game + league -----------------------------------------------
        box_game = QGroupBox("Game & League")
        form_game = QVBoxLayout(box_game)

        toggle_row = QHBoxLayout()
        self._btn_poe1 = QPushButton("Path of Exile 1")
        self._btn_poe2 = QPushButton("Path of Exile 2")
        for b in (self._btn_poe1, self._btn_poe2):
            b.setObjectName("gameButton")
            b.setCheckable(True)
        self._game_group = QButtonGroup(self)
        self._game_group.setExclusive(True)
        self._game_group.addButton(self._btn_poe1)
        self._game_group.addButton(self._btn_poe2)
        toggle_row.addWidget(self._btn_poe1)
        toggle_row.addWidget(self._btn_poe2)
        toggle_row.addStretch(1)
        form_game.addLayout(toggle_row)

        self._monitor_check = QCheckBox("Monitor this game (both games can run at once)")
        self._monitor_check.setToolTip("Tick for each game and Save & Apply to watch both")
        form_game.addWidget(self._monitor_check)

        league_row = QHBoxLayout()
        self._auto_league = QCheckBox("Auto (current league)")
        self._auto_league.setChecked(bool(self._cfg.game_cfg(self._editing_game).league_auto))
        self._auto_league.stateChanged.connect(self._on_auto_changed)
        league_row.addWidget(self._auto_league)
        self._league_combo = QComboBox()
        self._league_combo.setMinimumWidth(170)
        self._league_combo.setEnabled(not self._cfg.game_cfg(self._editing_game).league_auto)
        league_row.addWidget(self._league_combo, stretch=1)
        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setFixedWidth(34)
        self._refresh_btn.setToolTip("Refresh league list from poe.ninja")
        self._refresh_btn.clicked.connect(
            lambda: self.refresh_leagues_requested.emit(self._editing_game)
        )
        league_row.addWidget(self._refresh_btn)
        form_game.addLayout(league_row)

        start_row = QHBoxLayout()
        start_lbl = QLabel("League start")
        start_lbl.setToolTip("YYYY-MM-DD when the league launched; drives phase-adjusted "
                             "thresholds. Empty = auto (launch day when first seen, else neutral).")
        self._league_start_edit = QLineEdit()
        self._league_start_edit.setPlaceholderText("YYYY-MM-DD (optional — auto otherwise)")
        start_row.addWidget(start_lbl)
        start_row.addWidget(self._league_start_edit, stretch=1)
        form_game.addLayout(start_row)

        contact_row = QHBoxLayout()
        contact_lbl = QLabel("API contact")
        contact_lbl.setToolTip("poe.ninja asks every client for a contact in the User-Agent "
                               "(email or github handle). Required before redistributing.")
        self._contact_edit = QLineEdit()
        self._contact_edit.setPlaceholderText("email / github — sent as User-Agent contact")
        contact_row.addWidget(contact_lbl)
        contact_row.addWidget(self._contact_edit, stretch=1)
        form_game.addLayout(contact_row)
        root.addWidget(box_game)

        self._btn_poe1.toggled.connect(lambda on: on and self._on_game_switch("poe1"))
        self._btn_poe2.toggled.connect(lambda on: on and self._on_game_switch("poe2"))
        self._league_combo.currentTextChanged.connect(self._on_league_picked)

        # ---- categories ----------------------------------------------------
        box_watch = QGroupBox("Watched categories")
        v = QVBoxLayout(box_watch)
        self._cat_list = QListWidget()
        self._cat_list.setMinimumHeight(150)
        self._cat_list.itemChanged.connect(self._on_item_changed)
        v.addWidget(self._cat_list)
        root.addWidget(box_watch, stretch=1)

        # ---- detection -----------------------------------------------------
        box_detect = QGroupBox("Anomaly detection")
        form = QFormLayout(box_detect)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._engine_combo = QComboBox()
        self._engine_combo.addItem("Auto — phase-adjusted robust engine", "auto")
        self._engine_combo.addItem("Basic — fixed % jump / volume delta", "basic")
        self._engine_combo.setToolTip(
            "Auto: league-phase z-score, RVOL, EMA crossover, manipulation labels, "
            "alert budget. Falls back to the basic engine while baselines seed.")
        form.addRow("Detection engine", self._engine_combo)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(5, 240)
        self._interval_spin.setSuffix(" min")
        self._interval_spin.setToolTip("poe.ninja refreshes ~15 min (PoE 1) / ~hourly (PoE 2)")
        form.addRow("Poll interval (this game)", self._interval_spin)

        self._jump_spin = QDoubleSpinBox()
        self._jump_spin.setRange(1.0, 2000.0)
        self._jump_spin.setSuffix(" %")
        form.addRow("Price jump ≥", self._jump_spin)

        self._min_price_spin = QDoubleSpinBox()
        self._min_price_spin.setRange(0.0, 1_000_000.0)
        self._min_price_spin.setDecimals(4)
        form.addRow("Min price", self._min_price_spin)

        self._volume_spin = QDoubleSpinBox()
        self._volume_spin.setRange(0.0, 100.0)
        self._volume_spin.setSingleStep(0.5)
        self._volume_spin.setSpecialValueText("off")
        self._volume_spin.setToolTip("Current listings ≥ N × rolling average (items only)")
        form.addRow("Volume surge ≥", self._volume_spin)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Price jump OR volume surge", "any")
        self._mode_combo.addItem("Price jump AND volume surge", "all")
        form.addRow("Trigger mode", self._mode_combo)

        self._cooldown_spin = QSpinBox()
        self._cooldown_spin.setRange(5, 1440)
        self._cooldown_spin.setSuffix(" min")
        form.addRow("Alert cooldown", self._cooldown_spin)

        self._window_spin = QSpinBox()
        self._window_spin.setRange(3, 24)
        self._window_spin.setToolTip("Snapshots used to build the price baseline (median)")
        form.addRow("Baseline window", self._window_spin)
        root.addWidget(box_detect)

        # ---- alerts ---------------------------------------------------------
        box_alerts = QGroupBox("Alerts")
        form_a = QFormLayout(box_alerts)
        self._sound_check = QCheckBox()
        form_a.addRow("Sound alert", self._sound_check)
        self._popup_spin = QSpinBox()
        self._popup_spin.setRange(5, 300)
        self._popup_spin.setSuffix(" s")
        form_a.addRow("Popup lifetime", self._popup_spin)
        self._max_popups_spin = QSpinBox()
        self._max_popups_spin.setRange(1, 12)
        form_a.addRow("Max popups", self._max_popups_spin)
        root.addWidget(box_alerts)

        # ---- webhook automation ------------------------------------------------
        box_hook = QGroupBox("Webhook automation (Telegram / any JSON endpoint)")
        form_w = QFormLayout(box_hook)
        self._webhook_check = QCheckBox()
        form_w.addRow("Send alerts to webhook", self._webhook_check)
        self._webhook_url = QLineEdit()
        self._webhook_url.setPlaceholderText(
            "https://api.telegram.org/bot<TOKEN>/sendMessage  — or any POST endpoint")
        form_w.addRow("URL", self._webhook_url)
        self._webhook_format = QComboBox()
        self._webhook_format.addItem("Generic JSON POST", "generic")
        self._webhook_format.addItem("Telegram sendMessage", "telegram")
        form_w.addRow("Format", self._webhook_format)
        self._webhook_chat = QLineEdit()
        self._webhook_chat.setPlaceholderText("Telegram chat id (format=telegram only)")
        form_w.addRow("Chat id", self._webhook_chat)
        self._webhook_minprio = QDoubleSpinBox()
        self._webhook_minprio.setRange(0.0, 1000.0)
        self._webhook_minprio.setSingleStep(0.5)
        self._webhook_minprio.setToolTip("Only forward anomalies with priority ≥ this value")
        form_w.addRow("Min priority", self._webhook_minprio)
        root.addWidget(box_hook)

        # ---- buttons ---------------------------------------------------------
        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("Save & Apply")
        self._save_btn.setObjectName("primaryButton")
        self._save_btn.clicked.connect(self._save)
        btn_row.addWidget(self._save_btn)
        self._check_btn = QPushButton("Check now")
        self._check_btn.clicked.connect(self.check_now_requested.emit)
        btn_row.addWidget(self._check_btn)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self.pause_toggled.emit)
        btn_row.addWidget(self._pause_btn)
        root.addLayout(btn_row)

        link = QLabel(
            f'<a href="{BASE_URL}" style="color:#5aa9e6;">poe.ninja</a>'
            "  ·  closing this window keeps monitoring in the tray"
        )
        link.setOpenExternalLinks(True)
        link.setStyleSheet("color: #6b7280;")
        root.addWidget(link)

        self._load_from_config()
        self._set_game_toggle(self._editing_game)

    # ------------------------------------------------------------- state API
    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        color = STATE_COLORS.get(state, "#9aa4b2")
        text = STATE_TEXT.get(state, state)
        if state == "paused":
            self._pause_btn.blockSignals(True)
            self._pause_btn.setChecked(True)
            self._pause_btn.blockSignals(False)
            self._pause_btn.setText("Resume")
        else:
            self._pause_btn.blockSignals(True)
            self._pause_btn.setChecked(False)
            self._pause_btn.blockSignals(False)
            self._pause_btn.setText("Pause")
        if detail:
            text = f"{text} — {detail}"
        self._status_text.setText(text)
        self._status_dot.setPixmap(_status_dot_pixmap(color))
        self._status_dot.setFixedSize(12, 12)

    def set_leagues(self, game: str, league_ids: List[str]) -> None:
        self._leagues[game] = league_ids
        if game == self._editing_game:
            self._fill_league_combo()

    def flash_status(self, text: str) -> None:
        """Transient note (e.g. 'Fetching Currency…') that restores state text."""
        self._status_text.setText(text)

    # ------------------------------------------------------------- internals
    def _set_game_toggle(self, game: str) -> None:
        btn = self._btn_poe1 if game == "poe1" else self._btn_poe2
        btn.blockSignals(True)
        btn.setChecked(True)
        btn.blockSignals(False)
        other = self._btn_poe2 if game == "poe1" else self._btn_poe1
        other.blockSignals(True)
        other.setChecked(False)
        other.blockSignals(False)

    def _load_from_config(self) -> None:
        c = self._cfg
        self._engine_idx = self._engine_combo.findData(c.detector.engine)
        self._engine_combo.setCurrentIndex(max(self._engine_idx, 0))
        self._contact_edit.setText(c.network.contact)
        idx_w = self._webhook_format.findData(c.webhook.format)
        self._webhook_format.setCurrentIndex(max(idx_w, 0))
        self._webhook_check.setChecked(bool(c.webhook.enabled))
        self._webhook_url.setText(c.webhook.url)
        self._webhook_chat.setText(c.webhook.chat_id)
        self._webhook_minprio.setValue(float(c.webhook.min_priority))
        self._load_game_fields(self._editing_game)
        self._sound_check.setChecked(bool(c.alerts.sound))
        self._popup_spin.setValue(int(c.alerts.popup_seconds))
        self._max_popups_spin.setValue(int(c.alerts.max_popups))

    def _load_game_fields(self, game: str) -> None:
        c = self._cfg
        gc = c.game_cfg(game)
        self._monitor_check.setChecked(bool(gc.enabled))
        self._league_start_edit.setText(gc.league_start or "")
        self._interval_spin.setValue(int(gc.poll_interval_minutes))
        self._jump_spin.setValue(c.thresholds.jump_pct)
        self._min_price_spin.setValue(c.min_price_for(game))
        self._volume_spin.setValue(c.thresholds.volume_mult)
        idx = self._mode_combo.findData(c.thresholds.mode)
        self._mode_combo.setCurrentIndex(max(idx, 0))
        self._cooldown_spin.setValue(int(c.thresholds.cooldown_minutes))
        self._window_spin.setValue(int(c.thresholds.baseline_window))
        self._fill_categories(game)
        self._fill_league_combo()

    def _on_game_switch(self, game: str) -> None:
        # remember current UI check states for the outgoing game, then swap
        self._remember_checks(self._editing_game)
        self._editing_game = game  # which game's settings are shown
        self._load_game_fields(game)

    def _on_auto_changed(self, state) -> None:
        self._league_combo.setEnabled(self._auto_league.isChecked())
        if self._auto_league.isChecked():
            # ask poller for a fresh list; current league will land on top
            self.refresh_leagues_requested.emit(self._editing_game)

    def _on_league_picked(self, text: str) -> None:
        if not self._auto_league.isChecked() and text:
            self._cfg.game_cfg(self._editing_game).league = text

    def _fill_league_combo(self) -> None:
        game = self._editing_game
        gc = self._cfg.game_cfg(game)
        leagues = self._leagues.get(game, [])
        current = (gc.league or (leagues[0] if leagues else "") or "").strip()
        self._league_combo.blockSignals(True)
        self._league_combo.clear()
        items = leagues or ([current] if current else [])
        self._league_combo.addItems(items)
        if current:
            idx = self._league_combo.findText(current)
            self._league_combo.setCurrentIndex(max(idx, 0))
        self._league_combo.setEnabled(not self._auto_league.isChecked())
        self._league_combo.blockSignals(False)

    def _fill_categories(self, game: str) -> None:
        self._cat_list.blockSignals(True)
        self._cat_list.clear()
        enabled = set(self._cfg.watched(game)) | set(
            k for k, v in self._checks[game].items() if v
        )
        for cat_id, cat in CATEGORIES[game].items():
            item = QListWidgetItem(cat.label)
            item.setData(Qt.ItemDataRole.UserRole, cat_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = cat_id in enabled
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            if cat.source == "exchange":
                item.setToolTip(f"{cat.label} — bulk economy data (exchange API)")
            else:
                item.setToolTip(f"{cat.label} — listed items (stash API)")
            self._cat_list.addItem(item)
        self._cat_list.blockSignals(False)

    def _remember_checks(self, game: str) -> None:
        state_map: Dict[str, bool] = {}
        for i in range(self._cat_list.count()):
            item = self._cat_list.item(i)
            cat_id = item.data(Qt.ItemDataRole.UserRole)
            state_map[cat_id] = item.checkState() == Qt.CheckState.Checked
        self._checks[game] = state_map

    def _on_item_changed(self, _item) -> None:
        pass  # states are collected on save

    # ----------------------------------------------------------------- save
    def _save(self) -> None:
        c = self._cfg
        self._editing_game = "poe1" if self._btn_poe1.isChecked() else "poe2"
        gc = c.game_cfg(self._editing_game)
        gc.league_auto = self._auto_league.isChecked()
        gc.league = None if gc.league_auto else (self._league_combo.currentText().strip() or None)
        gc.enabled = self._monitor_check.isChecked()
        gc.league_start = self._league_start_edit.text().strip()
        gc.poll_interval_minutes = float(self._interval_spin.value())
        c.network.contact = self._contact_edit.text().strip()
        c.detector.engine = self._engine_combo.currentData() or "auto"
        c.thresholds.jump_pct = float(self._jump_spin.value())
        # min price is stored per game
        self._remember_checks(self._editing_game)
        c.thresholds.min_price[self._editing_game] = float(self._min_price_spin.value())
        c.thresholds.volume_mult = float(self._volume_spin.value())
        c.thresholds.mode = self._mode_combo.currentData() or "any"
        c.thresholds.cooldown_minutes = float(self._cooldown_spin.value())
        c.thresholds.baseline_window = int(self._window_spin.value())
        c.alerts.sound = self._sound_check.isChecked()
        c.alerts.popup_seconds = float(self._popup_spin.value())
        c.alerts.max_popups = int(self._max_popups_spin.value())
        c.webhook.enabled = self._webhook_check.isChecked()
        c.webhook.url = self._webhook_url.text().strip()
        c.webhook.format = self._webhook_format.currentData() or "generic"
        c.webhook.chat_id = self._webhook_chat.text().strip()
        c.webhook.min_priority = float(self._webhook_minprio.value())

        enabled = [cid for cid, on in self._checks[self._editing_game].items() if on]
        # keep registry order
        ordered = [cid for cid in CATEGORIES[self._editing_game] if cid in enabled]
        gc.watch = ordered or list(self._cfg.watched(self._editing_game))

        self.settings_saved.emit(c)

    # ------------------------------------------------------------- qt events
    def closeEvent(self, event) -> None:
        event.ignore()   # close-to-tray: hide instead of quitting
        self.hide()
