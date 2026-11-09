# ⚡ NinjaAlert

Windows tray app that watches **poe.ninja** (PoE 1 + PoE 2 in parallel), detects
price/liquidity anomalies with **league-phase-adjusted robust statistics**, and
pushes **clickable transparent popups**, a **History journal with outcome badges**,
a **one-click item mute**, and a **sound cue** — without flooding you.

```
PollWorker×2 (per game)  →  AnalysisWorker  →  governor  →  toasts / journal
  leagues + categories       robust z · RVOL     budget,     clickable cards
  ETag/304 · back-off        market drift split  mute/snooze + digest
  section discovery          manipulation score  dedup/key
```

## Install (one click)

1. Install **Python 3.11+** (tick "Add python.exe to PATH")
2. Download this repo → double-click **`install.bat`**
3. It builds `dist\NinjaAlert.exe`, launches it, done — it lives in the tray

Already built? Grab `dist\NinjaAlert.exe` from the
[Releases page](https://github.com/funkgriver/NinjaAlert/releases) — it's
portable, no Python needed.

## Daily work

- **Tray icon** — double-click opens settings; right-click menu: pause, check now, quit
- **Toasts** — `price → price (+%)` plus a **plain-English reason**
  ("price up 45% on 5x the usual volume — real demand"), not jargon.
  🔇 mutes the item for 30 min, ✕ dismisses, click opens poe.ninja
- **History tab** — every alert with a **confirmation badge** (✓ held N% /
  ✗ reverted / – gone), filters, 🔇 mute, click → poe.ninja
- **Mute** — annoying item? 🔇 anywhere: 30 minutes of silence, auto-expires

## Custom scenarios

Scenarios tab → **Add preset…** dropdown (hover for descriptions) or build your
own rule: match game + category + name substring, trigger on `pct` / `z` / `rvol`
/ `priority`, direction filter, per-item cooldown. Scenario alerts bypass the
alert budget and get a `SCENARIO·name` badge.

Typical recipes:

| Recipe | Rule | When to use |
|---|---|---|
| Divine moves | poe1 Currency ≥10% up | The main money pair moved |
| Dump watch | any ≤-15% down | Exit / short-check signal |
| Volume burst | RVOL ≥5 | Volume explodes before price does |
| Whale gear | poe1 uniques ≥15% up | Expensive items moving — follow the whales |
| League-start movers | poe2 Currency ≥20% up | Early poe2 economy swings |
| Essence spikes | poe1 Essences ≥25% up | Crafting demand surges |

Edit any preset freely after adding — cooldown, threshold, direction.

## Under the hood

- **Robust engine** — per-item z-score on hour-scaled log returns (median/MAD),
  liquidity weight, RVOL, EMA-trend confirmation, manipulation scoring
  (✅ ORGANIC / 🤔 AMBIGUOUS / ⚠ SUSPECT)
- **Noise guards** — unchanged ticks skipped, category-wide drift collapsed into
  one "market move" event, price-step floors, learned time-of-day volatility,
  empirical per-category alert-rate cap
- **Confirmation re-scan** — after an alert, its category is re-polled ~2 min
  later and the History card gets the outcome badge
- **Self-calibration** — classifier weights refit from measured outcomes every
  6 h (`detector.auto_calibrate` in config.json)
- **League rollover** — history wiped + baselines reseeded automatically, even
  if the app was off during the flip; section discovery re-sweeps the site index

## Tools

```bash
python tools/backtest.py        # score alerts: retained move at +2/6/24 h
python tools/calibrate.py       # refit weights + threshold suggestions
python tools/analyze.py         # journal summary, history stats
python selftest.py              # offline QA suite (~100 checks)
```

Be polite: poe.ninja has no SLA and blocks abusive clients — keep the API
contact filled and respect the poll interval. Personal-use tool.
