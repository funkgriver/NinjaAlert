"""Render the real alert popups to a PNG (no window appears on screen).

  python tools/render_alert_preview.py [out.png]

Builds three actual NotificationOverlay toasts through ToastManager (a robust
SUSPECT spike, a scenario hit, and the overflow digest), lets the fade-in
finish and composites them into one preview image. Uses the real formatting
pipeline (format_price, meta line, progress bar), so the picture matches what
you see in the tray app.
"""
from __future__ import annotations

import os
import sys
import time

# real fonts need a real platform plugin; the toasts flash on screen briefly
# and are dismissed right after the capture
if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "offscreen"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt                       # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPixmap # noqa: E402
from PyQt6.QtWidgets import QApplication          # noqa: E402

from analyzer import Alert                        # noqa: E402
from api_client import FetchMeta                  # noqa: E402
from appconfig import Config                      # noqa: E402
from models import Digest                         # noqa: E402
from ui_overlay import ToastManager, TOAST_GAP, TOAST_HEIGHT, TOAST_WIDTH  # noqa: E402


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "alert_preview.png")
    app = QApplication([])

    cfg = Config()
    cfg.alerts.sound = False                 # no beeps while rendering
    cfg.alerts.popup_seconds = 15.0

    meta = FetchMeta(game="poe1", league="Allflame", category="Currency",
                     source="exchange", primary="chaos", secondary="divine",
                     rates={"divine": 0.002353})
    meta_w = FetchMeta(game="poe1", league="Allflame", category="UniqueWeapon",
                       source="item", primary="chaos", secondary="divine",
                       rates={"divine": 0.002353})

    def alert(name, cat, src, old, new, label, phase, z, rvol=None, price_display="both"):
        cfg.alerts.price_display = price_display
        return Alert(game="poe1", league="Allflame", category=cat, name=name,
                     old_price=old, new_price=new, pct=(new / old - 1.0) * 100.0,
                     list_count=None, vol_ratio=rvol, icon=None,
                     details_id=name.lower().replace(" ", "-"), source=src,
                     ts=time.time(), key=f"poe1:x:{cat}:{name}", label=label,
                     phase=phase, z_adj=z)

    mgr = ToastManager(lambda: cfg, ROOT)
    mgr.show_alert(alert("Divine Orb", "Currency", "exchange", 100.0, 148.0,
                         "SUSPECT", "Stabilization", 4.2, rvol=3.1), meta)
    mgr.show_alert(alert("Headhunter", "UniqueWeapon", "item", 4100.0, 5250.0,
                         "SCENARIO·Whale watch", "Mature", 3.6,
                         price_display="secondary"), meta_w)
    from models import Digest, DigestItem
    ditems = tuple(DigestItem(
        key=f"poe1:i:UniqueArmour:det{i}:", name=name,
        category="poe1:item:UniqueArmour", price=price, prev_price=prev,
        pct=(price / prev - 1.0) * 100.0, direction=1 if price >= prev else -1,
        label=label, priority=5.0 - i)
        for i, (name, prev, price, label) in enumerate([
            ("Lich's Circle (Shaper/Warlord)", 12.0, 19.5, "SUSPECT"),
            ("Shagreen Boots (Hunter)", 4.1, 6.8, "AMBIGUOUS"),
            ("Sacrificial Garb (Warlord)", 210.0, 305.0, "ORGANIC"),
            ("Vixen's Entrapment", 88.0, 124.0, "SUSPECT"),
        ]))
    mgr.show_digest(Digest(count=31,
                           names=tuple(it.name for it in ditems),
                           categories=(("poe1:item:BaseType", 31), ("poe1:item:ValdoMap", 1)),
                           items=ditems), "poe1", "Allflame")

    # let the 150 ms fade-in finish so the capture shows the settled look
    t0 = time.time()
    while time.time() - t0 < 0.45:
        app.processEvents()
        time.sleep(0.02)

    overlays = list(mgr._active)[:3]
    canvas = QPixmap(TOAST_WIDTH + 20, (TOAST_HEIGHT + TOAST_GAP) * len(overlays) + 10)
    canvas.fill(QColor("#0b0d13"))
    p = QPainter(canvas)
    y = 5
    for ov in reversed(overlays):            # digest was shown last, stack bottom-up
        shot = ov.grab()
        p.drawPixmap(10, y, shot)
        y += TOAST_HEIGHT + TOAST_GAP
    p.end()
    mgr.dismiss_all()
    app.processEvents()
    ok = canvas.save(out, "PNG")
    print(("wrote " if ok else "FAILED ") + os.path.abspath(out))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
