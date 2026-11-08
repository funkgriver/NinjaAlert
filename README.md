# ⚡ NinjaAlert

Windows tray app that watches **poe.ninja** (PoE 1 + PoE 2 in parallel), detects
price/liquidity anomalies with **league-phase-adjusted robust statistics**, and
pushes **clickable transparent popups**, a **history journal**, webhook messages
and a **sound cue** — without flooding you.

```
PollWorker×2 (per game)  →  AnalysisWorker  →  governor  →  toasts / journal
  leagues + categories       robust z · RVOL     budget,     clickable cards
  ETag/304 · back-off        market drift split  cooldowns   + digest
  section discovery          manipulation score  dedup/key
```

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py          # tray app
python main.py --debug  # console logging + settings window
```

Build: `build.bat` → `dist\NinjaAlert.exe` (single file). Config lives next to
the exe (`config.json`, created on first run) and holds your Telegram/webhook
secrets — it is gitignored, never committed; `config.example.json` documents
the schema.

## Setup (2 minutes)

1. Launch → tray icon → double-click opens the (resizable) settings window.
2. **API contact** — an email/GitHub handle poe.ninja can identify you by.
3. **Games** — tick PoE 1 and/or PoE 2, pick watched categories, optional
   league start date (empty = auto rollover detection).
4. Save & Apply. Alerts start after ~3 polls (baselines seeding).

## How alerts work

- **Robust engine** — per-item z-score on hour-scaled log returns (median/MAD,
  spikes can't poison the baseline), liquidity weight, RVOL, EMA-trend
  confirmation, manipulation classification: ✅ ORGANIC / 🤔 AMBIGUOUS / ⚠ SUSPECT.
- **Noise guards** — unchanged ticks never reach the engine, category-wide
  drift collapses into one "market move" event, the empirical per-category
  threshold caps raw alerts at a daily target, price-step floors kill
  quantization artifacts, time-of-day volatility is learned.
- **Alert budget** — token bucket (5 tokens, 1/2 min), escalating per-item
  cooldowns, overflow folds into one interactive digest: click it to expand
  the folded moves, click a row to open poe.ninja.
- **Confirmation re-scan** — after an alert, only its category is re-polled
  (~2 min) and the card in History gets its outcome badge: ✓ held N%,
  ✗ reverted, – gone. Nothing is lost: digest-folded moves land in History
  marked "folded by budget".
- **History tab** — every alert as a toast replica from `alerts.jsonl`:
  filters, live refresh, click → poe.ninja.
- **Self-calibration** — classifier weights refit from measured forward
  outcomes (did the move hold?), guarded by sample gates and holdout log-loss.

## Tools

```bash
python tools/backtest.py        # score alerts: retained move at +2/6/24 h
python tools/calibrate.py       # refit weights + threshold suggestions
python tools/analyze.py         # journal summary, |z| buckets, history stats
python tools/diag_returns.py    # zero-return share, sqrt(dt) scaling check
python tools/diag_alert_rate.py # raw (pre-governor) alert rate replay
python tools/mem_stress.py      # RSS growth under load
```

## Files it keeps (bounded by design)

| File | Bound |
|---|---|
| `history-*.sqlite3` | only price changes stored, 14-day retention, hourly WAL checkpoint, wiped on league rollover (history is league-scoped; leagues live ≤ ~4 months) |
| `alerts.jsonl` | rotated to `.old` at 5 MB and on every league change |
| `ninjaalert.log` | 512 KB × 2 rotations |
| `config.json` | yours, gitignored — holds tokens, never commit it |

## Layout

`main.py` (tray/controller) · `analysis_worker.py` (engine-owner QThread) ·
`poller.py` (per-game QThread + section discovery) · `api_client.py` (ETag/304,
retries) · `advanced_analyzer.py` (phase model, robust engine, governor,
HistoryStore) · `analyzer.py` (basic delta engine) · `calibration.py`
(outcome-labelled refit) · `scenarios.py` (user alert rules) · `webhook.py`
(journal + webhook + confirm records) · `ui_overlay.py` / `ui_settings.py` /
`ui_history.py` · `selftest.py` (offline QA suite).

Be polite: poe.ninja has no SLA and blocks abusive clients — keep the API
contact filled, respect the poll interval. Personal-use tool.
