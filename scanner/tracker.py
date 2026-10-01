"""Paper-trade tracking for every alert.

For each alert we record the price at +5m, +15m, +60m and +4h, and simulate
the suggested trade: did the price reach the take-profit or the stop-loss
first? The result is reported after the estimated fees and slippage that
were attached to the alert.

Prices come from two places: the regular scans (tokens are re-checked for a
while after they trend) and an extra DexScreener lookup each loop for any
alert still inside its 4-hour window.

Honest limitation: we only see the price every ~90 seconds, not every
trade, so a quick spike through TP or SL between two looks can be missed.
Stop-losses are filled at the price we actually saw (which may be worse
than the SL level, like a real gap); take-profits are filled at the TP
level (never better), so results lean conservative.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

CHECKPOINTS = (("price_5m", 5 * 60), ("price_15m", 15 * 60),
               ("price_60m", 60 * 60), ("price_4h", 4 * 3600))
HORIZON = 4 * 3600          # trade is closed at the 4h price if neither TP nor SL hit
GRACE = 30 * 60             # extra time allowed to collect the final price

HIT_TP, HIT_SL, HIT_EXPIRED, HIT_OPEN, HIT_NO_DATA = "TP", "SL", "EXPIRED", "OPEN", "NO_DATA"
FINISHED = (HIT_TP, HIT_SL, HIT_EXPIRED)


def _max_lag(offset: float) -> float:
    """How late a reading may be and still count for a checkpoint
    (3 min for +5m, 7.5 min for +15m, 30 min for +60m and +4h)."""
    return max(180.0, min(offset * 0.5, GRACE))


def evaluate(alert, observations: list[tuple[float, float]], now: float,
             default_cost_pct: float) -> dict:
    """Pure function: alert row + price readings -> outcome fields."""
    start = alert["ts"]
    entry = alert["price_usd"]
    tp = alert["take_profit_usd"]
    sl = alert["stop_loss_usd"]
    readings = sorted((t, p) for t, p in observations if start < t <= start + HORIZON + GRACE)
    obs = [(t, p) for t, p in readings if t <= start + HORIZON]   # the trade's lifetime

    out: dict = {name: None for name, _ in CHECKPOINTS}
    for name, offset in CHECKPOINTS:
        target = start + offset
        for t, p in readings:
            if t >= target:
                if t <= target + _max_lag(offset):
                    out[name] = p
                break

    hit, hit_ts, gross = HIT_OPEN, None, None
    if entry and tp and sl:
        for t, p in obs:
            if p >= tp:
                hit, hit_ts, gross = HIT_TP, t, (tp / entry - 1) * 100
                break
            if p <= sl:
                hit, hit_ts, gross = HIT_SL, t, (p / entry - 1) * 100
                break
        if hit == HIT_OPEN and now >= start + HORIZON:
            if out["price_4h"]:          # close at the 4-hour price
                exit_p = out["price_4h"]
                exit_t = next(t for t, p in readings if t >= start + HORIZON)
                hit, hit_ts, gross = HIT_EXPIRED, exit_t, (exit_p / entry - 1) * 100
            elif obs and now >= start + HORIZON + GRACE:   # no 4h reading: last one we had
                last_t, last_p = obs[-1]
                hit, hit_ts, gross = HIT_EXPIRED, last_t, (last_p / entry - 1) * 100
            elif now >= start + HORIZON + GRACE:
                hit = HIT_NO_DATA
    else:
        hit = HIT_NO_DATA

    cost = alert["est_cost_pct"]
    cost = default_cost_pct if cost is None else cost
    out.update(hit=hit, hit_ts=hit_ts,
               gross_return_pct=None if gross is None else round(gross, 3),
               net_return_pct=None if gross is None else round(gross - cost, 3),
               updated_ts=now)
    return out


class Tracker:
    def __init__(self, cfg: Config, storage: Storage, dexscreener=None):
        self.cfg = cfg
        self.storage = storage
        self.dexscreener = dexscreener

    def update(self, now: float | None = None) -> int:
        """Fetch fresh prices for live alerts and re-evaluate. Returns # evaluated."""
        now = now or time.time()
        alerts = self.storage.alerts_to_track(since=now - HORIZON - GRACE)
        if not alerts:
            return 0
        self._fetch_prices([a for a in alerts if now - a["ts"] <= HORIZON + GRACE], now)
        for a in alerts:
            obs = self.storage.price_observations(a["id"], a["chain"], a["token_address"],
                                                  a["ts"], a["ts"] + HORIZON + GRACE)
            fields = evaluate(a, obs, now, self.cfg.trade.round_trip_fee_pct)
            self.storage.save_outcome(a["id"], **fields)
        self.storage.commit()
        return len(alerts)

    def _fetch_prices(self, alerts: list[sqlite3.Row], now: float) -> None:
        if self.dexscreener is None or not alerts:
            return
        by_chain: dict[str, list[sqlite3.Row]] = {}
        for a in alerts:
            # Skip tokens the scan just captured - we already have that price.
            if self.storage.recently_snapshotted(a["chain"], a["token_address"], now - 60):
                continue
            by_chain.setdefault(a["chain"], []).append(a)
        for chain, items in by_chain.items():
            addresses = sorted({a["token_address"] for a in items})
            try:
                snaps = self.dexscreener.snapshots(chain, addresses)
            except Exception:
                log.exception("Paper-trade price lookup failed for %s", chain)
                continue
            for a in items:
                snap = snaps.get(a["token_address"])
                if snap and snap.price_usd:
                    self.storage.add_alert_price(a["id"], now, snap.price_usd)
