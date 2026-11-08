"""NinjaAlert - compact settings / control panel.

Tabs:
  Monitoring  - status header, PoE 1 / PoE 2 game toggle, league selector
                (auto-detect from poe.ninja), watched categories per game,
                thresholds, alert behavior, webhook automation
  Scenarios   - user-defined automation rules (metric thresholds + matchers)
  Status      - live diagnostics snapshot + on-demand automated checks

The window closes to tray (hide), it never quits the app.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import replace as dc_replace
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api_client import BASE_URL
from appconfig import Config, ScenarioCfg, VALID_DIRECTIONS, VALID_METRICS, VALID_OPS, \
    app_dir
from categories import CATEGORIES, labels_for
from diagnostics import format_result, run_checks
from ui_history import HistoryTab

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


# Ready-made scenario templates (Scenarios tab -> Presets...), sane for PoE
# market quirks: spikes are usually UP, dumps are rarer but real, volume
# bursts often precede price moves, |z| filters out tiny league-start noise.
SCENARIO_PRESETS = (
    ("Divine moves — poe1 Currency ≥10% up",
     dict(name="Divine moves", game="poe1", category="Currency",
          metric="pct", op=">=", value=10.0, direction="up", cooldown_minutes=60.0)),
    ("Dump watch — any ≤-15% down",
     dict(name="Dump watch", game="poe1", metric="pct", op="<=", value=-15.0,
          direction="down", cooldown_minutes=60.0)),
    ("Volume burst — RVOL ×5+",
     dict(name="Volume burst", game="poe1", metric="rvol", op=">=", value=5.0,
          cooldown_minutes=90.0)),
    ("High-conviction — |z| ≥4.5",
     dict(name="High-conviction z", game="poe1", metric="z_abs", op=">=",
          value=4.5, cooldown_minutes=120.0)),
    ("Top priority — ≥3",
     dict(name="Top priority", game="poe1", metric="priority", op=">=",
          value=3.0, cooldown_minutes=45.0)),
    ("Whale gear — poe1 uniques ≥15% up",
     dict(name="Whale gear", game="poe1", category="UniqueWeapon",
          metric="pct", op=">=", value=15.0, direction="up", cooldown_minutes=60.0)),
)


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


class ScenarioDialog(QDialog):
    """Add / edit one automation scenario."""

    def __init__(self, parent: QWidget, scen: Optional[ScenarioCfg] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit scenario" if scen else "New scenario")
        self.setMinimumWidth(420)
        s = scen or ScenarioCfg()

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._name = QLineEdit(str(s.name))
        form.addRow("Name", self._name)

        self._game = QComboBox()
        self._game.addItem("Path of Exile 1", "poe1")
        self._game.addItem("Path of Exile 2", "poe2")
        idx = self._game.findData(s.game)
        self._game.setCurrentIndex(max(idx, 0))
        form.addRow("Game", self._game)

        self._category = QComboBox()
        self._fill_categories(str(s.category))
        self._game.currentIndexChanged.connect(
            lambda _: self._fill_categories(self._category.currentData()))
        form.addRow("Category", self._category)

        self._pattern = QLineEdit(str(s.name_pattern))
        self._pattern.setPlaceholderText("optional: match part of the item name")
        form.addRow("Name contains", self._pattern)

        self._metric = QComboBox()
        for m in VALID_METRICS:
            self._metric.addItem({"pct": "Price change %", "z_abs": "|z-score|",
                                  "rvol": "Relative volume", "priority": "Priority"}[m], m)
        idx = self._metric.findData(s.metric)
        self._metric.setCurrentIndex(max(idx, 0))
        form.addRow("Metric", self._metric)

        self._op = QComboBox()
        for o in VALID_OPS:
            self._op.addItem(o, o)
        idx = self._op.findData(s.op)
        self._op.setCurrentIndex(max(idx, 0))
        form.addRow("Comparison", self._op)

        self._value = QDoubleSpinBox()
        self._value.setRange(-1_000_000.0, 1_000_000.0)
        self._value.setDecimals(3)
        self._value.setValue(float(s.value))
        form.addRow("Threshold", self._value)

        self._direction = QComboBox()
        for d in VALID_DIRECTIONS:
            self._direction.addItem({"any": "Any direction", "up": "Price up",
                                     "down": "Price down"}[d], d)
        idx = self._direction.findData(s.direction)
        self._direction.setCurrentIndex(max(idx, 0))
        form.addRow("Direction", self._direction)

        self._cooldown = QSpinBox()
        self._cooldown.setRange(5, 10080)
        self._cooldown.setSuffix(" min")
        self._cooldown.setValue(int(s.cooldown_minutes))
        form.addRow("Cooldown (per item)", self._cooldown)

        self._enabled = QCheckBox("Scenario enabled")
        self._enabled.setChecked(bool(s.enabled))
        form.addRow(self._enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _fill_categories(self, selected: Optional[str]) -> None:
        game = self._game.currentData()
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem("Any category", "*")
        for cid, label in labels_for(game).items():
            self._category.addItem(label, cid)
        if selected:
            idx = self._category.findData(selected)
            self._category.setCurrentIndex(max(idx, 0))
        self._category.blockSignals(False)

    def values(self) -> ScenarioCfg:
        return ScenarioCfg(
            name=self._name.text().strip() or "scenario",
            game=str(self._game.currentData()),
            category=str(self._category.currentData() or "*"),
            name_pattern=self._pattern.text().strip(),
            metric=str(self._metric.currentData()),
            op=str(self._op.currentData()),
            value=float(self._value.value()),
            direction=str(self._direction.currentData()),
            cooldown_minutes=float(self._cooldown.value()),
            enabled=self._enabled.isChecked(),
        )


class SettingsWindow(QWidget):
    settings_saved = pyqtSignal(object)       # new Config
    check_now_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    refresh_leagues_requested = pyqtSignal(str)
    checks_ready = pyqtSignal(list)           # [CheckResult]

    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._editing_game = "poe1"
        self._leagues: Dict[str, List[str]] = {}
        self._checks: Dict[str, Dict[str, bool]] = {"poe1": {}, "poe2": {}}
        self._state = "starting"
        self._paused = False
        self._status_provider: Optional[Callable[[], dict]] = None
        self._scen_draft: List[ScenarioCfg] = [dc_replace(s) for s in config.scenarios]

        self.setWindowTitle("NinjaAlert — poe.ninja spike monitor")
        self.setMinimumWidth(560)
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

        # ---- tabs ----------------------------------------------------------
        self._tabs = QTabWidget()
        page_mon = QWidget()
        form_mon = QVBoxLayout(page_mon)
        form_mon.setContentsMargins(2, 2, 2, 2)
        form_mon.setSpacing(10)

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
        form_mon.addWidget(box_game)

        self._btn_poe1.toggled.connect(lambda on: on and self._on_game_switch("poe1"))
        self._btn_poe2.toggled.connect(lambda on: on and self._on_game_switch("poe2"))
        self._league_combo.currentTextChanged.connect(self._on_league_picked)

        # ---- categories ----------------------------------------------------
        box_watch = QGroupBox("Watched categories")
        v = QVBoxLayout(box_watch)
        self._cat_search = QLineEdit()
        self._cat_search.setClearButtonEnabled(True)
        self._cat_search.setPlaceholderText(
            "Filter…  e.g. 'frag' → Fragments · 's' → Fragments, Cards, Belts")
        self._cat_search.textChanged.connect(self._filter_categories)
        v.addWidget(self._cat_search)
        self._cat_list = QListWidget()
        self._cat_list.setMinimumHeight(150)
        self._cat_list.itemChanged.connect(self._on_item_changed)
        v.addWidget(self._cat_list)
        form_mon.addWidget(box_watch, stretch=1)

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
        form_mon.addWidget(box_detect)

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
        self._price_display_combo = QComboBox()
        self._price_display_combo.addItem("Primary (chaos PoE1 / divine PoE2)", "primary")
        self._price_display_combo.addItem("Secondary (divine PoE1 / exalted PoE2)", "secondary")
        self._price_display_combo.addItem("Both + conversion hint", "both")
        self._price_display_combo.setToolTip("How alert popups render prices")
        form_a.addRow("Price display", self._price_display_combo)
        form_mon.addWidget(box_alerts)

        # ---- webhook automation ------------------------------------------------
        box_hook = QGroupBox("Webhook automation (any JSON endpoint)")
        form_w = QFormLayout(box_hook)
        self._webhook_check = QCheckBox()
        form_w.addRow("Send alerts to webhook", self._webhook_check)
        self._webhook_url = QLineEdit()
        self._webhook_url.setPlaceholderText("https://your-endpoint/alerts  (POST, full anomaly JSON)")
        form_w.addRow("URL", self._webhook_url)
        self._webhook_minprio = QDoubleSpinBox()
        self._webhook_minprio.setRange(0.0, 1000.0)
        self._webhook_minprio.setSingleStep(0.5)
        self._webhook_minprio.setToolTip("Only forward anomalies with priority ≥ this value")
        form_w.addRow("Min priority", self._webhook_minprio)
        form_mon.addWidget(box_hook)

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
        form_mon.addLayout(btn_row)

        link = QLabel(
            f'<a href="{BASE_URL}" style="color:#5aa9e6;">poe.ninja</a>'
            "  ·  closing this window keeps monitoring in the tray"
        )
        link.setOpenExternalLinks(True)
        link.setStyleSheet("color: #6b7280;")
        form_mon.addWidget(link)
        form_mon.addStretch(1)

        self._tabs.addTab(page_mon, "Monitoring")
        self._tabs.addTab(self._build_scenarios_tab(), "Scenarios")
        self._tabs.addTab(self._build_status_tab(), "Status")
        self._history = HistoryTab(lambda: self._cfg.alerts.price_display)
        self._tabs.addTab(self._history, "History")
        root.addWidget(self._tabs, stretch=1)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3000)
        self._status_timer.timeout.connect(self._refresh_status)
        self.checks_ready.connect(self._on_checks_ready)

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

    # ------------------------------------------------------------ scenarios
    def _build_scenarios_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(2, 2, 2, 2)
        hint = QLabel("Custom alert rules evaluated on every anomaly, independent of the "
                      "alert budget. Matched alerts show a SCENARIO·<name> badge and go "
                      "through the journal/webhook like normal alerts.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280;")
        v.addWidget(hint)

        self._scen_list = QListWidget()
        self._scen_list.setMinimumHeight(180)
        self._scen_list.itemChanged.connect(self._on_scenario_toggled)
        v.addWidget(self._scen_list, stretch=1)

        row = QHBoxLayout()
        btn_add = QPushButton("Add…")
        btn_add.clicked.connect(self._scenario_add)
        btn_edit = QPushButton("Edit…")
        btn_edit.clicked.connect(self._scenario_edit)
        btn_del = QPushButton("Remove")
        btn_del.clicked.connect(self._scenario_remove)
        self._preset_combo = QComboBox()
        self._preset_combo.addItem("Add preset…", None)
        for label, kwargs in SCENARIO_PRESETS:
            self._preset_combo.addItem(label, kwargs)
        self._preset_combo.setToolTip("Ready-made scenario templates (dropdown)")
        self._preset_combo.currentIndexChanged.connect(self._on_preset_pick)
        for b in (btn_add, btn_edit, btn_del, self._preset_combo):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        note = QLabel("Changes apply with Save & Apply.")
        note.setStyleSheet("color: #6b7280;")
        v.addWidget(note)
        return page

    def _on_preset_pick(self, idx: int) -> None:
        kwargs = self._preset_combo.itemData(idx)
        if kwargs:
            self._add_preset(kwargs)
        self._preset_combo.blockSignals(True)
        self._preset_combo.setCurrentIndex(0)   # reset to placeholder
        self._preset_combo.blockSignals(False)

    def _add_preset(self, kwargs: dict) -> None:
        self._scen_draft.append(ScenarioCfg(**kwargs))
        self._reload_scenarios()

    @staticmethod
    def _scenario_summary(sc: ScenarioCfg) -> str:
        cat = "any" if sc.category == "*" else sc.category
        pat = f" · name~'{sc.name_pattern}'" if sc.name_pattern else ""
        direction = {"any": "", "up": " · up", "down": " · down"}[sc.direction]
        return (f"{sc.name} — {sc.game}/{cat}: {sc.metric} {sc.op} "
                f"{sc.value:g}{direction}{pat} · cooldown {sc.cooldown_minutes:.0f}m")

    def _reload_scenarios(self) -> None:
        self._scen_list.blockSignals(True)
        self._scen_list.clear()
        for sc in self._scen_draft:
            item = QListWidgetItem(self._scenario_summary(sc))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sc.enabled
                               else Qt.CheckState.Unchecked)
            if not sc.enabled:
                item.setForeground(QColor("#6b7280"))
            self._scen_list.addItem(item)
        self._scen_list.blockSignals(False)

    def _on_scenario_toggled(self, item: QListWidgetItem) -> None:
        idx = self._scen_list.row(item)
        if 0 <= idx < len(self._scen_draft):
            self._scen_draft[idx].enabled = item.checkState() == Qt.CheckState.Checked
            item.setForeground(QColor("#e6e8ee") if self._scen_draft[idx].enabled
                               else QColor("#6b7280"))

    def _scenario_add(self) -> None:
        dlg = ScenarioDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._scen_draft.append(dlg.values())
            self._reload_scenarios()

    def _scenario_edit(self) -> None:
        idx = self._scen_list.currentRow()
        if not (0 <= idx < len(self._scen_draft)):
            return
        dlg = ScenarioDialog(self, self._scen_draft[idx])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._scen_draft[idx] = dlg.values()
            self._reload_scenarios()

    def _scenario_remove(self) -> None:
        idx = self._scen_list.currentRow()
        if 0 <= idx < len(self._scen_draft):
            del self._scen_draft[idx]
            self._reload_scenarios()

    # --------------------------------------------------------- status tab
    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(8)

        live = QGroupBox("Live status (refreshes every 3 s)")
        form = QFormLayout(live)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._live_labels: Dict[str, QLabel] = {}
        for key, title in (("engine", "Engine"), ("poe1", "PoE 1"), ("poe2", "PoE 2"),
                           ("detail", "Last event")):
            lbl = QLabel("—")
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._live_labels[key] = lbl
            form.addRow(title, lbl)
        v.addWidget(live)

        checks = QGroupBox("Automated checks")
        cv = QVBoxLayout(checks)
        self._checks_list = QListWidget()
        self._checks_list.setMinimumHeight(190)
        cv.addWidget(self._checks_list)
        row = QHBoxLayout()
        self._btn_checks = QPushButton("Run checks")
        self._btn_checks.clicked.connect(lambda: self._run_checks(probe_api=False))
        row.addWidget(self._btn_checks)
        self._btn_api = QPushButton("Probe poe.ninja")
        self._btn_api.setToolTip("Live reachability check (leagues endpoint, short timeout)")
        self._btn_api.clicked.connect(lambda: self._run_checks(probe_api=True))
        row.addWidget(self._btn_api)
        row.addStretch(1)
        cv.addLayout(row)
        v.addWidget(checks, stretch=1)
        return page

    def set_status_provider(self, fn: Optional[Callable[[], dict]]) -> None:
        self._status_provider = fn

    def set_history_source(self, fn: Callable[..., list],
                           rev_fn: Optional[Callable[[], int]] = None) -> None:
        self._history.set_provider(fn, rev_fn)

    def _refresh_status(self) -> None:
        if self._status_provider is None:
            return
        try:
            snap = self._status_provider()
        except Exception:
            log.exception("status provider failed")
            return
        engine = self._live_labels["engine"]
        engine.setText(
            f"{snap.get('engine', '?')} · auto-calibration "
            f"{'on' if snap.get('auto_calibrate') else 'off'} · "
            f"scenarios: {snap.get('scenarios', 0)}")
        stats = snap.get("worker_stats") or {}
        states = snap.get("poller_states") or {}
        leagues = snap.get("leagues") or {}
        for game in ("poe1", "poe2"):
            lbl = self._live_labels[game]
            if game in stats:
                robust, basic = stats[game]
                lbl.setText(f"{states.get(game, '?')} · {leagues.get(game, '')} · "
                            f"{robust} robust + {basic} basic items tracked")
            else:
                lbl.setText(f"{states.get(game, '?')} · worker warming up")
        self._live_labels["detail"].setText(str(snap.get("last_detail") or "—"))

    def _run_checks(self, probe_api: bool) -> None:
        self._btn_checks.setEnabled(False)
        self._btn_api.setEnabled(False)
        self._checks_list.clear()
        self._checks_list.addItem(QListWidgetItem(
            "running checks…" if not probe_api else "probing poe.ninja…"))
        thread = threading.Thread(target=self._checks_worker,
                                  args=(probe_api, self._cfg), daemon=True)
        thread.start()

    def _checks_worker(self, probe_api: bool, cfg) -> None:
        try:
            results = run_checks(cfg, app_dir(), probe_api=probe_api)
        except Exception as exc:               # never break the GUI thread
            log.exception("diagnostics failed")
            from diagnostics import CheckResult
            results = [CheckResult("Automated checks", False, f"{type(exc).__name__}: {exc}")]
        self.checks_ready.emit(results)

    def _on_checks_ready(self, results: list) -> None:
        self._btn_checks.setEnabled(True)
        self._btn_api.setEnabled(True)
        self._checks_list.clear()
        for r in results:
            item = QListWidgetItem(format_result(r))
            item.setForeground(QColor("#41d98c") if r.ok else QColor("#e5534b"))
            self._checks_list.addItem(item)

    # ------------------------------------------------------------- qt events
    def showEvent(self, event) -> None:
        self._status_timer.start()
        self._refresh_status()
        self._history.activate()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._status_timer.stop()
        self._history.deactivate()
        super().hideEvent(event)

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
        self._webhook_check.setChecked(bool(c.webhook.enabled))
        self._webhook_url.setText(c.webhook.url)
        self._webhook_minprio.setValue(float(c.webhook.min_priority))
        self._load_game_fields(self._editing_game)
        self._sound_check.setChecked(bool(c.alerts.sound))
        self._popup_spin.setValue(int(c.alerts.popup_seconds))
        self._max_popups_spin.setValue(int(c.alerts.max_popups))
        self._scen_draft = [dc_replace(s) for s in c.scenarios]
        self._reload_scenarios()
        idx_pd = self._price_display_combo.findData(c.alerts.price_display)
        self._price_display_combo.setCurrentIndex(max(idx_pd, 0))

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
        self._filter_categories(self._cat_search.text())

    def _filter_categories(self, text: str) -> None:
        """Live type-to-filter on the category list: case-insensitive substring
        on the label or the API id ('frag' → Fragments, 's' → FragmentS,
        CardS, BeltS…). Hidden items keep their check state and are still
        collected on save."""
        needle = (text or "").strip().lower()
        for i in range(self._cat_list.count()):
            item = self._cat_list.item(i)
            haystack = f"{item.text()} {item.data(Qt.ItemDataRole.UserRole)}".lower()
            item.setHidden(bool(needle) and needle not in haystack)

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
        c.alerts.price_display = self._price_display_combo.currentData() or "both"
        c.webhook.enabled = self._webhook_check.isChecked()
        c.webhook.url = self._webhook_url.text().strip()
        c.webhook.min_priority = float(self._webhook_minprio.value())
        c.scenarios = list(self._scen_draft)

        enabled = [cid for cid, on in self._checks[self._editing_game].items() if on]
        # keep registry order
        ordered = [cid for cid in CATEGORIES[self._editing_game] if cid in enabled]
        gc.watch = ordered or list(self._cfg.watched(self._editing_game))

        self.settings_saved.emit(c)

    # ------------------------------------------------------------- qt events
    def closeEvent(self, event) -> None:
        event.ignore()   # close-to-tray: hide instead of quitting
        self.hide()
