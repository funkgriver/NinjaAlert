# ⚡ NinjaAlert

Windows tray app that monitors **poe.ninja** (PoE 1 + PoE 2, both games in parallel),
detects price/liquidity anomalies with **league-phase-adjusted robust statistics**,
and pushes **draggable transparent popups**, optional **Telegram/webhook** messages
and a **sound cue** — without flooding you.

Built against the 2026 poe.ninja economy API (exchange + stash item overviews,
ETag-cached, per [poe.ninja/docs/api](https://poe.ninja/docs/api)).

```
PollWorker×2 (QThreads, one per game)  →  AnalysisWorker (QThread, engine owner)
  leagues + watched categories             per game: robust z · RVOL · EMA crossover
  ETag / 304 · 429+Retry-After back-off    · manipulation M · AlertGovernor
  section auto-discovery                   · scenarios · basic engine (warmup)
                                                              ↓
              popups · beep · webhook (Telegram) · alerts.jsonl · SQLite (WAL)
```

All heavy work (robust statistics, SQLite writes, warm-start replay) runs on the
worker thread — the GUI only dispatches alerts. The worker queue, icon cache and
history replay are bounded by design, and process RSS is logged every 30 minutes.

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py          # tray app
python main.py --debug  # console logging + settings window
```

Build the exe: `build.bat` → `dist\NinjaAlert.exe` (single file, no deps).
Config lives next to the exe (`config.json`, created on first run; v1 configs migrate automatically).
**`config.json` is personal** (it will hold your Telegram token and contact) — it is
gitignored and never committed; `config.example.json` documents the full schema.

## Base setup (2 minutes)

1. Launch → tray icon appears. Double-click → Settings.
2. **API contact** — type an email/GitHub handle (poe.ninja's politeness contract).
3. Games — tick **Monitor this game** for PoE 1 and/or PoE 2 (both can run at once),
   pick watched categories, set **League start** `YYYY-MM-DD` (optional: empty = auto —
   rollover detection records launch day; unknown age falls back to a neutral day-30 profile).
4. **Detection engine** — leave on **Auto**.
5. Save & Apply. First alerts appear once baselines exist (see below).

## Settings tabs (v2.1)

- **Monitoring** — everything from the base setup: games, league, watched categories
  (with a **type-to-filter box** — `frag` → Fragments, `s` → Fragments, Cards, Belts…),
  thresholds, **price display** (chaos / divine / both), alerts and webhook automation.
- **Scenarios** — your own alert rules: match game/category/name substring, trigger on
  price-change %, |z-score|, relative volume or priority (≥/≤), optional direction filter,
  per-item cooldown. Scenario alerts bypass the alert budget and are tagged
  `SCENARIO·<name>` in popups, `alerts.jsonl` and webhooks.
- **Status** — live snapshot (poller state, leagues, robust/basic items tracked, last
  event, **process RSS**) plus one-click **automated checks** (weights, SQLite WAL +
  index, journal, icon asset, log tail) and a live **poe.ninja probe**.
- **Auto sections** — once a day the app parses poe.ninja's economy index: brand-new
  categories are probed against the API, registered and auto-watched; watched sections that
  keep answering 404 are auto-cleared (with a tray notification).
- **Telegram shortcut** — pasting just the bot token (`123456:ABC-…`) instead of the full
  `sendMessage` URL works too; a missing chat id is skipped gracefully with a warning.

## Auto alerts

- **Engine `auto` (default)** — league-phase model scales the alert threshold with the
  league's age (launch z*≈4.7 → stabilization ≈3.05 → late-league ≈3.9) and lengthens
  EMA memory as markets calm. Per item it computes a robust z-score on hour-scaled log
  returns (median/MAD baseline, single spikes can't poison it), a liquidity weight
  (thin markets need bigger moves), RVOL, supply ratio, cross-rate basis stress and an
  EMA-trend confirmation, then classifies every spike: **ORGANIC / AMBIGUOUS / SUSPECT**
  (suspected cornering/fake listings). Until an item has 24 stored returns, the basic
  % jump/volume engine covers it, so alerts start after ~3 polls even on first run.
- **Alert budget** — ranked by priority, admitted through a token bucket (5 tokens,
  1 per 2 min); re-alerts need ≥1.5× the last priority; overflow collapses into one
  digest popup ("+7 more moves · Scarab ×3 …").
- **Webhook/Telegram** — Settings → *Webhook automation*: tick, paste
  `https://api.telegram.org/bot<TOKEN>/sendMessage`, set **Format = Telegram** + your
  chat id, set **Min priority** (start at 0; raise if too chatty). Generic JSON POST
  works for any other endpoint. Rate-limited, failures never block the UI.
- **Pause/Check-now** from the tray menu; both games switchable independently.

## Analyzing

- `history-poe1.sqlite3` / `history-poe2.sqlite3` (next to the exe, 14-day retention)
  store every tick — warm start rebuilds baselines instantly after restart.
- `alerts.jsonl` — one JSON line per admitted alert (price, z_adj, RVOL, label,
  priority, phase) — feed for refitting thresholds/weights.

```bash
# how many alerts per label / category:
python -c "import json,collections;rows=[json.loads(l) for l in open('alerts.jsonl',encoding='utf-8')];print(collections.Counter((r['label'],r['category']) for r in rows))"

# top priorities:
python -c "import json;rows=sorted([json.loads(l) for l in open('alerts.jsonl',encoding='utf-8')],key=lambda r:-r['priority'])[:10];[print(r['priority'],r['name'],r['price'],r['label'],r['phase']) for r in rows]"
```

Recheck live schemas anytime: `python selftest.py --live` · one real headless cycle:
`python tools/smoke_cycle.py [poe2]`.

## Tuning, calibration & memory

- **Weight calibration** — the manipulation classifier's weights can be refit from the
  stored history (pseudo-labelled from the model's own confident decisions, with min-sample
  gates, weight clamps and a holdout log-loss gate):
  ```bash
  python tools/calibrate.py            # preview: suggested weights + Youden-J thresholds
  python tools/calibrate.py --apply    # write improved weights into config.json (backup first)
  ```
  Optional in-app refit: `detector.auto_calibrate` in `config.json` (default off),
  every `detector.calibrate_interval_hours`.
- **Memory** — replay/queues/caches are bounded (streamed warm start, capped worker queue,
  LRU icon cache, hourly baseline pruning). Verify anytime:
  `python tools/mem_stress.py [--cycles 120]` pushes real snapshot volume through the
  pipeline and reports RSS growth. RSS is also logged every 30 min and shown in the
  Status tab's "Process memory" check.

## Layout

`main.py` (tray/controller) · `analysis_worker.py` (engine-owner QThread) ·
`poller.py` (per-game QThread + section discovery/auto-clear) · `api_client.py`
(2026 API, ETag/304, jittered retries, Retry-After) · `categories.py` (46+23
category registry) · `analyzer.py` (basic engine) · `advanced_analyzer.py`
(phase model, robust z, manipulation, governor, SQLite history) ·
`calibration.py` + `tools/calibrate.py` (weight refit) · `scenarios.py`
(user alert rules) · `discovery.py` (site-index parsing) · `diagnostics.py`
(automated checks) · `models.py` (immutable contracts) · `webhook.py`
(Telegram/JSON + journal) · `ui_overlay.py` / `ui_settings.py` ·
`selftest.py` (offline QA suite).

Be polite: poe.ninja has no SLA and blocks abusive clients. Personal-use tool.
