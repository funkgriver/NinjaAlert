# NinjaAlert diagnostics (branch fix/analysis-improvements)

_Source: real local history, `dist/history-poe1.sqlite3` (484,334 tick pairs) and
`dist/history-poe2.sqlite3` (empty), recorded 2026-11-07/08. Tools: `tools/diag_returns.py`,
`tools/diag_alert_rate.py` (both read-only). League start unknown -> phase model at default day 30 ("Mature")._

```

history-poe1.sqlite3: 484334 consecutive pairs
  unchanged-price pairs: 83.5%
    gap (min)    pairs   zero%    med|r|  med|r| nz  med|r|/sqrt(dt)
          0-5    29674    99%    0.0000     0.0352           0.1271
         5-15    23444    96%    0.0000     0.0392           0.1118
        15-30   182855    85%    0.0000     0.0098           0.0167
        30-60   216387    84%    0.0000     0.0224           0.0307
       60-180     6495    27%    0.0218     0.0454           0.0440
         >180    25479    50%    0.0002     0.0808           0.0236
  reading: flat last column = sqrt(dt) scaling fits; strongly falling = short gaps inflate z.

history-poe2.sqlite3: 0 consecutive pairs


history-poe1.sqlite3
  evaluated ticks: 157940   span: 0.2 days
  raw alerts (pre-governor): 6154  ->  29637.6 per day, 38.96 per 1000 ticks
  mean z* = 3.05; a Gaussian model would give 2.28 per 1000 ticks (before the min-move gate)
  categories : poe1:item:SkillGem=3988, poe1:item:UniqueArmour=748, poe1:item:UniqueWeapon=400, poe1:item:UniqueAccessory=330, poe1:item:UniqueJewel=174, poe1:exchange:Scarab=129, poe1:exchange:Currency=128, poe1:exchange:Fragment=103
  phases     : Mature=6154
  labels     : ORGANIC=4807, AMBIGUOUS=1282, SUSPECT=65
  |z| buckets: >=6=5225, 3.5-4.5=377, 4.5-6=364, <3.5=188
  reading: raw rate far above the Gaussian figure = heavy tails; the governor is then doing the noise filtering, not the detector.

history-poe2.sqlite3
  not enough history after the warm-up window
```

## Reading

- **83.5% of consecutive poe1 tick pairs have an unchanged price.** Zero returns drag the
  per-item MAD towards the floor; the next real move then produces a huge z (item 8).
- **Short gaps inflate z:** `med|r|/sqrt(dt)` is ~0.12-0.13 for 0-15 min gaps (confirmation
  re-scans) vs ~0.02-0.03 for 15-60 min gaps - a 4-8x overstatement of move significance (item 9).
- **Raw alert rate:** 6,154 pre-governor alerts in ~4.8 h of evaluated history = 38.96/1000 ticks
  vs 2.28/1000 expected from a Gaussian at z*=3.05 - **17x heavy-tail inflation**. 85% of raw
  alerts sit in the |z|>=6 bucket, i.e. the MAD scale collapsed rather than the move being real.
  SkillGem alone accounts for 65% (level/quality steps are quantized prices - item 5).
- poe2 has no history yet (game disabled during recording) - rerun when data exists.

These numbers motivated blocks B (robust scale/thresholds), C (unchanged-tick skip,
category-scoped re-scan) and D (outcome-based labels, priority).
