# ⚡ NinjaAlert

Windows tray app that monitors **poe.ninja** (PoE 1 + PoE 2, both games in parallel),
detects price/liquidity anomalies with **league-phase-adjusted robust statistics**,
and pushes **draggable transparent popups**, optional **Telegram/webhook** messages
and a **sound cue** — without flooding you.

Built against the 2026 poe.ninja economy API (exchange + stash item overviews,
ETag-cached, per [poe.ninja/docs/api](https://poe.ninja/docs/api)).

```
PollWorker×2 (QThreads, one per game)  →  Engine (per game)  →  AlertGovernor
  leagues + watched categories            auto: robust z · RVOL · EMA crossover
  ETag / 304 · 429 back-off               · manipulation M · token bucket + digest
                                          basic: % jump + listing surge (warmup)
                                                              ↓
                              popups · beep · webhook (Telegram) · alerts.jsonl · SQLite
```

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py          # tray app
python main.py --debug  # console logging + settings window
```

Build the exe: `build.bat` → `dist\NinjaAlert.exe` (single file, no deps).
Config lives next to the exe (`config.json`, created on first run; v1 configs migrate automatically).

## Base setup (2 minutes)

1. Launch → tray icon appears. Double-click → Settings.
2. **API contact** — type an email/GitHub handle (poe.ninja's politeness contract).
3. Games — tick **Monitor this game** for PoE 1 and/or PoE 2 (both can run at once),
   pick watched categories, set **League start** `YYYY-MM-DD` (optional: empty = auto —
   rollover detection records launch day; unknown age falls back to a neutral day-30 profile).
4. **Detection engine** — leave on **Auto**.
5. Save & Apply. First alerts appear once baselines exist (see below).

## Settings tabs (v2.1)

- **Monitoring** — everything from the base setup: games, league, watched categories,
  thresholds, alerts and webhook automation.
- **Scenarios** — your own alert rules: match game/category/name substring, trigger on
  price-change %, |z-score|, relative volume or priority (≥/≤), optional direction filter,
  per-item cooldown. Scenario alerts bypass the alert budget and are tagged
  `SCENARIO·<name>` in popups, `alerts.jsonl` and webhooks.
- **Status** — live snapshot (poller state, leagues, robust/basic items tracked, last event)
  plus one-click **automated checks** (weights, SQLite WAL + index, journal, icon asset,
  log tail) and a live **poe.ninja probe**.
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

## Layout

`main.py` (tray/controller) · `poller.py` (per-game QThread) · `api_client.py`
(2026 API, ETag, retries) · `categories.py` (46+23 category registry) ·
`analyzer.py` (basic engine) · `advanced_analyzer.py` (phase model, robust z,
manipulation, governor, SQLite) · `models.py` (immutable contracts) ·
`webhook.py` (Telegram/JSON + journal) · `ui_overlay.py` / `ui_settings.py`.

Be polite: poe.ninja has no SLA and blocks abusive clients. Personal-use tool.
