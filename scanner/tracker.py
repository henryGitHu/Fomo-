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
from .strategy import (HIT_EXPIRED, HIT_NO_DATA, HIT_OPEN, HIT_RUNNING, HIT_SL, HIT_TP,
                       HIT_TRAIL)

log = logging.getLogger(__name__)

CHECKPOINTS = (("price_5m", 5 * 60), ("price_15m", 15 * 60),
               ("price_60m", 60 * 60), ("price_4h", 4 * 3600))
HORIZON = 4 * 3600          # trade is closed at the 4h price if neither TP nor SL hit
GRACE = 30 * 60             # extra time allowed to collect the final price

FINISHED = (HIT_TP, HIT_SL, HIT_EXPIRED, HIT_TRAIL)
STILL_OPEN = (None, HIT_OPEN, HIT_RUNNING)


def _col(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _max_lag(offset: float) -> float:
    """How late a reading may be and still count for a checkpoint
    (3 min for +5m, 7.5 min for +15m, 30 min for +60m and +4h)."""
    return max(180.0, min(offset * 0.5, GRACE))


def first_tp_cross(alert, observations) -> float | None:
    """Time the price first reached take-profit (before any stop-loss), else None."""
    start, tp, sl = alert["ts"], alert["take_profit_usd"], alert["stop_loss_usd"]
    if not (tp and sl):
        return None
    for t, p in sorted(observations):
        if not (start < t <= start + HORIZON):
            continue
        if p <= sl:
            return None
        if p >= tp:
            return t
    return None


def evaluate(alert, observations: list[tuple[float, float]], now: float,
             default_cost_pct: float) -> dict:
    """Pure function: alert row + price readings -> outcome fields.

    Runner trades (alert["runner_state"] == 1): at take-profit the trade isn't
    sold; a trailing stop follows the peak (never below the take-profit level)
    and the trade closes when the price falls trail_pct from its peak, or at
    runner_hold_hours."""
    start = alert["ts"]
    entry = alert["price_usd"]
    tp = alert["take_profit_usd"]
    sl = alert["stop_loss_usd"]
    runner = _col(alert, "runner_state") == 1
    trail = float(_col(alert, "trail_pct", 8.0))
    run_horizon = float(_col(alert, "runner_hold_hours", 12.0)) * 3600 if runner else HORIZON
    window = max(HORIZON, run_horizon)
    readings = sorted((t, p) for t, p in observations if start < t <= start + window + GRACE)
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
            if p >= tp and runner:
                hit, hit_ts, gross = _run(readings, t, tp, trail, entry, start + run_horizon, now)
                break
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


def _run(readings, t0, tp, trail_pct, entry, end, now):
    """Trailing-stop phase of a runner, from the take-profit crossing at t0."""
    peak = tp
    last_t = t0
    for t, p in readings:
        if t < t0 or t > end:
            continue
        last_t, last_p = t, p
        peak = max(peak, p)
        stop = max(tp, peak * (1 - trail_pct / 100))
        if t > t0 and p <= stop:
            return HIT_TRAIL, t, (p / entry - 1) * 100       # sold at the price actually seen
    if now >= end:
        # Time limit: close at the last price seen in the window.
        prices = [(t, p) for t, p in readings if t0 <= t <= end]
        t, p = prices[-1] if prices else (t0, tp)
        return HIT_TRAIL, t, (p / entry - 1) * 100
    return HIT_RUNNING, None, None


class Tracker:
    def __init__(self, cfg: Config, storage: Storage, dexscreener=None):
        self.cfg = cfg
        self.storage = storage
        self.dexscreener = dexscreener

    def _max_window(self) -> float:
        return max(HORIZON, self.cfg.strategy.runner_max_hold_hours * 3600) + GRACE

    def update(self, now: float | None = None) -> int:
        """Fetch fresh prices for live alerts and re-evaluate. Returns # evaluated."""
        now = now or time.time()
        alerts = self.storage.alerts_to_track(since=now - self._max_window())
        if not alerts:
            return 0
        live = [a for a in alerts if _col(a, "cur_hit") in STILL_OPEN
                and now - a["ts"] <= self._max_window()]
        self._fetch_prices(live, now)
        for a in alerts:
            obs = self.storage.price_observations(a["id"], a["chain"], a["token_address"],
                                                  a["ts"], a["ts"] + self._max_window())
            if _col(a, "strategy_trade") == 1 and _col(a, "runner_state") is None:
                crossed = first_tp_cross(a, obs)
                if crossed is not None:
                    state = 1 if self._runner_ok(a, now) else 0
                    self.storage.set_runner_state(a["id"], state)
                    a = dict(a)
                    a["runner_state"] = state
                    if state:
                        log.info("Runner: %s %s hit its target with strong momentum - trailing",
                                 a["chain"], a["symbol"])
            fields = evaluate(a, obs, now, self.cfg.trade.round_trip_fee_pct)
            self.storage.save_outcome(a["id"], **fields)
        self.storage.commit()
        return len(alerts)

    def _runner_ok(self, alert, now: float) -> bool:
        """Is momentum still strong right now? (rule: only run past the target
        when the data strongly supports it)"""
        s = self.cfg.strategy
        if not (s.enabled and s.runner_enabled and _col(alert, "strategy_trade") == 1):
            return False
        from .scoring import momentum_score
        hist = self.storage.snapshot_history(alert["chain"], alert["token_address"],
                                             since=now - self.cfg.momentum.baseline_minutes * 60 - 600,
                                             before=now + 1)
        if not hist or now - hist[-1].ts > 600:
            return False                      # no fresh data -> take the profit
        snap = hist[-1]
        mom = momentum_score(snap, hist[:-1], self.cfg.momentum)
        return (mom.score >= s.runner_min_momentum
                and (snap.price_change_m5 or 0) > 0
                and (mom.buy_ratio_m5 or 0) >= 0.55
                and not mom.giant_candle)

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
            seen = set()
            for a in items:
                snap = snaps.get(a["token_address"])
                if snap and snap.price_usd:
                    self.storage.add_alert_price(a["id"], now, snap.price_usd)
                    if a["token_address"] not in seen:     # keep momentum data for runner decisions
                        snap.ts = now
                        self.storage.add_snapshots([snap])
                        seen.add(a["token_address"])
