"""Self-learning: review every closed paper trade and improve from them.

What happens:

1. **Every trade gets a review** when it closes: how far it went up and down
   while open, what went well, what went wrong and the lesson. The bot keeps
   watching the price for the rest of the 4-hour window after the exit, so
   it can tell "the stop was too tight - it recovered and hit the target"
   from "the stop saved us - it kept falling".
2. **Every few hours it re-analyses ALL past trades:**
   * Exits: each risk level's past trades are replayed with other
     take-profit / stop-loss levels (using the prices actually recorded).
     If another pair would clearly have done better - overall and in both
     the older and the newer half of the trades - the level is moved a few
     points toward it.
   * Entries: trades are grouped by what the token looked like at entry
     ("already up 40%+ in the last hour", "thin liquidity", "safety
     unverified", ...). A group that keeps losing makes similar tokens need
     extra score points before they're traded. A group that recovers gets
     its penalty lifted again.
3. Every change is sent to Telegram with the reason, and kept in a log.

Guardrails it can never cross: safety checks, the daily loss limit, trade
size, max open trades and min_trade_score are never loosened. Learned
take-profits / stop-losses stay inside learning.tp_*/sl_* ranges. Nothing
changes until there are enough trades, and each change is small.

    python -m scanner.learning            what it has learned + recent reviews
    python -m scanner.learning --retrain  re-analyse every trade right now
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime

from .config import Config, ConfigError, StrategyLevel, load_config
from .storage import Storage
from .strategy import CLOSED, HIT_EXPIRED, HIT_SL, HIT_TP, HIT_TRAIL, day_start, trade_pnl
from .tracker import GRACE, HORIZON, evaluate

log = logging.getLogger(__name__)

KEY_STATE = "learned_state"
KEY_LAST_RETRAIN = "last_learn_ts"
RISKS = ("LOW", "MEDIUM", "HIGH")
TP_GRID = (5, 6, 8, 10, 12, 15, 18, 20, 25, 30)
SL_GRID = (3, 4, 5, 6, 7, 8, 10, 12, 15)
MIN_IMPROVEMENT = 1.0        # avg net % per trade a new exit pair must add
PATTERN_STEP = 5.0           # most a pattern's score adjustment moves per re-analysis
SHRINK = 10                  # small groups are pulled toward zero: avg * n / (n + SHRINK)
SEND_REVIEW_IF_CLOSED_WITHIN = 2 * 3600

# --------------------------------------------------------------------------
# what a token looked like at entry
# --------------------------------------------------------------------------


def features_from_row(row, now: float) -> dict:
    """Snapshot of a scan row at the moment of the trade (stored with the alert)."""
    s, m, c = row.snap, row.momentum, row.composite
    parts = c.parts if c else {}
    return {
        "score": c.score if c else None, "risk": c.risk if c else None,
        "momentum": round(m.score, 1), "momentum_parts": dict(m.parts),
        "safety_margin": parts.get("safety"), "social": parts.get("social"),
        "safety": row.safety.status if row.safety else None,
        "chain": s.chain, "liquidity": s.liquidity_usd,
        "mcap": s.market_cap_usd or s.fdv_usd, "age_min": s.age_minutes(now),
        "pc_m5": s.price_change_m5, "pc_h1": s.price_change_h1, "pc_h6": s.price_change_h6,
        "vol_ratio": None if m.volume_ratio is None else round(m.volume_ratio, 2),
        "buy_ratio_m5": m.buy_ratio_m5, "buy_ratio_h1": m.buy_ratio_h1,
        "giant_candle": bool(m.giant_candle), "signals": list(m.signals),
        "hour": datetime.fromtimestamp(now).hour,
    }


def features_for(storage: Storage, alert) -> dict:
    """Stored features, or the best we can rebuild for trades made before
    the learner existed (alert columns + the scan snapshot at entry)."""
    raw = _get(alert, "features")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    try:
        signals = json.loads(_get(alert, "signals") or "[]")
    except ValueError:
        signals = []
    f = {"score": _get(alert, "score"), "risk": _get(alert, "risk"), "chain": alert["chain"],
         "liquidity": _get(alert, "liquidity_usd"), "safety": _get(alert, "safety_status"),
         "giant_candle": "giant_candle" in signals, "signals": signals,
         "hour": datetime.fromtimestamp(alert["ts"]).hour}
    hist = storage.snapshot_history(alert["chain"], alert["token_address"],
                                    since=alert["ts"] - 600, before=alert["ts"] + 1)
    if hist:
        s = hist[-1]
        f.update(pc_m5=s.price_change_m5, pc_h1=s.price_change_h1, pc_h6=s.price_change_h6,
                 age_min=s.age_minutes(alert["ts"]), mcap=s.market_cap_usd or s.fdv_usd)
        if s.buys_m5 is not None and s.sells_m5 is not None and s.buys_m5 + s.sells_m5:
            f["buy_ratio_m5"] = s.buys_m5 / (s.buys_m5 + s.sells_m5)
    return f


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _num(f: dict, key: str):
    v = f.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ge(key, x):
    return lambda f: None if _num(f, key) is None else _num(f, key) >= x


def _lt(key, x):
    return lambda f: None if _num(f, key) is None else _num(f, key) < x


def _between(key, lo, hi):
    return lambda f: None if _num(f, key) is None else lo <= _num(f, key) < hi


def _is(key, value):
    return lambda f: None if f.get(key) is None else str(f.get(key)).upper() == value


# Entry patterns the learner judges: (key, plain-English label, test).
# A test returns None when the trade has no data for it.
PATTERNS = [
    ("pump_h1_40", "already up 40%+ in the last hour", _ge("pc_h1", 40)),
    ("pump_h1_15_40", "up 15-40% in the last hour", _between("pc_h1", 15, 40)),
    ("calm_h1", "up less than 15% in the last hour", _lt("pc_h1", 15)),
    ("hot_m5", "up 10%+ in the last 5 minutes", _ge("pc_m5", 10)),
    ("giant_candle", "the move was one giant candle", lambda f: f.get("giant_candle")),
    ("vol_3x", "volume 3x+ its normal pace", _ge("vol_ratio", 3)),
    ("weak_buyers", "buyers under 55% of recent trades", _lt("buy_ratio_m5", 0.55)),
    ("strong_buyers", "buyers 65%+ of recent trades", _ge("buy_ratio_m5", 0.65)),
    ("young", "pool less than 1 hour old", _lt("age_min", 60)),
    ("age_1_6h", "pool 1-6 hours old", _between("age_min", 60, 360)),
    ("older_6h", "pool more than 6 hours old", _ge("age_min", 360)),
    ("thin_liq", "liquidity under $50k", _lt("liquidity", 50_000)),
    ("mid_liq", "liquidity $50k-$150k", _between("liquidity", 50_000, 150_000)),
    ("deep_liq", "liquidity $150k+", _ge("liquidity", 150_000)),
    ("risk_low", "LOW risk rating", _is("risk", "LOW")),
    ("risk_medium", "MEDIUM risk rating", _is("risk", "MEDIUM")),
    ("risk_high", "HIGH risk rating", _is("risk", "HIGH")),
    ("unverified", "safety UNVERIFIED", _is("safety", "UNVERIFIED")),
    ("social", "Telegram buzz 50+", _ge("social", 50)),
    ("score_78_84", "score 78-84", _between("score", 78, 85)),
    ("score_85_89", "score 85-89", _between("score", 85, 90)),
    ("score_90", "score 90+", _ge("score", 90)),
    ("night", "bought between midnight and 6am", _between("hour", 0, 6)),
    ("morning", "bought between 6am and noon", _between("hour", 6, 12)),
    ("afternoon", "bought between noon and 6pm", _between("hour", 12, 18)),
    ("evening", "bought between 6pm and midnight", _between("hour", 18, 24)),
]


def matching_patterns(f: dict) -> list[tuple[str, str]]:
    out = [(k, label) for k, label, test in PATTERNS if test(f)]
    if f.get("chain"):
        out.append((f"chain_{f['chain']}", f"on {f['chain']}"))
    return out


# --------------------------------------------------------------------------
# reviewing one trade
# --------------------------------------------------------------------------

TAG_TEXT = {      # short names for summaries
    "quick_win": "quick win", "runner_win": "runner win", "left_on_table": "kept rising after we sold",
    "near_stop": "nearly stopped out first", "stop_too_tight": "stopped out, then recovered",
    "almost_won": "nearly hit target, then reversed", "late_entry": "late entry after a big run-up",
    "fast_dump": "dumped within minutes", "gap_through_stop": "fell through the stop",
    "never_up": "never went green", "giant_candle": "bought one giant candle",
    "weak_buyers": "weak buying", "thin_liquidity": "thin liquidity", "costs_heavy": "costs too heavy",
    "went_nowhere": "went nowhere in 4h", "stop_saved": "stop saved us from a bigger drop",
    "unverified": "safety unverified",
}

LESSONS = {
    "stop_too_tight": "The stop was inside this coin's normal swings. The learner will test wider "
                      "stops for this risk level.",
    "almost_won": "The target was a bit far for this move. The learner will test smaller targets.",
    "late_entry": "Buying after a big 1-hour run-up often means buying the top. If this keeps "
                  "losing, the learner will make these entries need a higher score.",
    "fast_dump": "It dumped right after the alert - the pump was probably ending.",
    "gap_through_stop": "Prices can drop through a stop in one move. Small trade sizes keep this survivable.",
    "never_up": "The move was over before we got in.",
    "giant_candle": "One giant candle is often a single big buyer, not a real trend.",
    "weak_buyers": "Sellers were already outnumbering buyers - not a strong setup.",
    "thin_liquidity": "Thin liquidity means bigger swings and worse fills.",
    "costs_heavy": "Fees and slippage were large next to the target - small targets need deep liquidity.",
    "went_nowhere": "No follow-through. Momentum faded without a clear move.",
    "unverified": "Safety couldn't be verified - extra risk.",
    "left_on_table": "A strong runner. Trades like this are what the runner rule is for.",
    "near_stop": "It came close to the stop before working - a little luck was involved.",
    "quick_win": "Clean, fast win - the kind of setup to repeat.",
    "runner_win": "The runner rule paid off: it rode the trend past the target.",
    "stop_saved": "The stop did its job - it kept falling after we sold.",
}

LOSS_PRIORITY = ("stop_too_tight", "almost_won", "late_entry", "fast_dump", "gap_through_stop",
                 "never_up", "giant_candle", "weak_buyers", "thin_liquidity", "costs_heavy",
                 "went_nowhere", "unverified", "stop_saved")
WIN_PRIORITY = ("left_on_table", "runner_win", "near_stop", "quick_win", "costs_heavy")
EXIT_NAMES = {HIT_TP: "take-profit", HIT_SL: "stop-loss", HIT_EXPIRED: "time limit (4h)",
              HIT_TRAIL: "runner trailing stop"}


def review_end(alert) -> float:
    """Until when we keep watching the price after a trade closed."""
    return max(alert["ts"] + HORIZON + GRACE, (_get(alert, "hit_ts") or alert["ts"]) + GRACE)


def review_trade(t, obs: list[tuple[float, float]], f: dict) -> dict:
    """Pure function: closed trade + prices + entry features -> review fields."""
    entry = t["price_usd"]
    tp_pct = (t["take_profit_usd"] / entry - 1) * 100 if entry and t["take_profit_usd"] else 0.0
    sl_pct = (1 - t["stop_loss_usd"] / entry) * 100 if entry and t["stop_loss_usd"] else 0.0
    exit_ts = t["hit_ts"]
    gross = t["gross_return_pct"] or 0.0
    net = t["net_return_pct"] if t["net_return_pct"] is not None else gross
    cost = gross - net
    hit = t["hit"]

    def pct(p):
        return (p / entry - 1) * 100

    during = [pct(p) for ts, p in obs if t["ts"] < ts <= exit_ts]
    after = [pct(p) for ts, p in obs if exit_ts < ts <= review_end(t)]
    mfe = max(during + [gross, 0.0]) if hit != HIT_SL else max(during + [0.0])
    mae = min(during + [min(gross, 0.0)])
    a_hi = max(after) if after else None
    a_lo = min(after) if after else None
    minutes = (exit_ts - t["ts"]) / 60
    tags, good, bad = [], [], []

    if net > 0:
        if hit == HIT_TRAIL:
            tags.append("runner_win")
            good.append(f"Ran past the +{tp_pct:.0f}% target and sold at {gross:+.1f}% with the trailing stop.")
        else:
            good.append(f"Reached the {gross:+.1f}% exit in {minutes:.0f} minutes.")
            if hit == HIT_TP and minutes <= 30:
                tags.append("quick_win")
        if a_hi is not None and a_hi - gross >= max(10.0, tp_pct):
            tags.append("left_on_table")
            bad.append(f"After selling it kept going to {a_hi:+.0f}% - it could have run further.")
        if sl_pct and mae <= -0.7 * sl_pct:
            tags.append("near_stop")
            bad.append(f"Dipped to {mae:+.1f}% first, close to the -{sl_pct:.0f}% stop.")
        if gross > 0 and cost >= 0.3 * gross:
            tags.append("costs_heavy")
            bad.append(f"Fees and slippage took {cost:.1f}% of the {gross:.1f}% gain.")
    else:
        if hit == HIT_SL:
            if minutes <= 10:
                tags.append("fast_dump")
                bad.append(f"Hit the stop {minutes:.0f} minutes after the alert.")
            if sl_pct and gross < -(sl_pct + 3):
                tags.append("gap_through_stop")
                bad.append(f"Fell through the stop: sold at {gross:+.1f}% instead of -{sl_pct:.0f}%.")
            elif sl_pct:
                good.append(f"The stop kept the loss to {gross:+.1f}%.")
            if tp_pct and mfe >= 0.6 * tp_pct:
                tags.append("almost_won")
                bad.append(f"Was up {mfe:+.1f}% (target +{tp_pct:.0f}%) before reversing to the stop.")
            elif mfe < 1:
                tags.append("never_up")
                bad.append("Never went meaningfully green after the alert.")
            if tp_pct and a_hi is not None and a_hi >= max(0.75 * tp_pct, sl_pct):
                tags.append("stop_too_tight")
                bad.append(f"After the stop it climbed to {a_hi:+.0f}% - " +
                           (f"the +{tp_pct:.0f}% target would have been hit." if a_hi >= tp_pct
                            else f"nearly to the +{tp_pct:.0f}% target."))
            elif a_lo is not None and a_lo <= gross - 10:
                tags.append("stop_saved")
                good.append(f"Selling was right: it fell to {a_lo:+.0f}% afterwards.")
        else:
            tags.append("went_nowhere")
            bad.append(f"No target or stop in time; closed at {gross:+.1f}% (best was {mfe:+.1f}%).")
            if tp_pct and mfe >= 0.6 * tp_pct:
                tags.append("almost_won")

    # What the token looked like at entry.
    pc_h1, br, liq = _num(f, "pc_h1"), _num(f, "buy_ratio_m5"), _num(f, "liquidity")
    if pc_h1 is not None and pc_h1 >= 40:
        tags.append("late_entry")
        (bad if net <= 0 else good).append(
            f"Bought after it was already up {pc_h1:.0f}% in the hour"
            + (" - likely late." if net <= 0 else " and it still worked."))
    if f.get("giant_candle"):
        tags.append("giant_candle")
        if net <= 0:
            bad.append("The entry signal was one giant candle, not a steady climb.")
    if br is not None:
        if br < 0.55:
            tags.append("weak_buyers")
            if net <= 0:
                bad.append(f"Only {br * 100:.0f}% of recent trades were buys.")
        elif br >= 0.65:
            good.append(f"Buyers were {br * 100:.0f}% of recent trades at entry.")
    if liq is not None and liq < 50_000:
        tags.append("thin_liquidity")
        if net <= 0:
            bad.append(f"Thin liquidity (${liq:,.0f}).")
    if (f.get("safety") or "").upper() == "UNVERIFIED":
        tags.append("unverified")
        if net <= 0:
            bad.append("Safety checks couldn't be verified.")
    elif (f.get("safety") or "").upper() == "PASS":
        good.append("Passed the safety checks.")
    if tp_pct and cost >= 0.25 * tp_pct and "costs_heavy" not in tags:
        tags.append("costs_heavy")
        bad.append(f"Costs ({cost:.1f}%) were large next to the +{tp_pct:.0f}% target.")

    order = WIN_PRIORITY if net > 0 else LOSS_PRIORITY
    lesson_tag = next((tg for tg in order if tg in tags), None)
    lesson = LESSONS.get(lesson_tag, "Normal result for this setup - nothing stands out yet."
                         if net <= 0 else "Worked as planned.")
    return dict(mfe_pct=round(mfe, 2), mae_pct=round(mae, 2),
                after_high_pct=None if a_hi is None else round(a_hi, 2),
                after_low_pct=None if a_lo is None else round(a_lo, 2),
                minutes_to_exit=round(minutes, 1), tags=json.dumps(tags),
                positives=json.dumps(good), negatives=json.dumps(bad), lesson=lesson)


def review_message(t, r: dict) -> str:
    e = html.escape
    net = t["net_return_pct"] or 0.0
    usd = trade_pnl(t)
    icon = "🟢" if net > 0 else "🔴"
    lines = [f"🔍 <b>Trade review: {e(t['symbol'] or '?')}</b> ({e(t['chain'])}) · "
             f"{EXIT_NAMES.get(t['hit'], t['hit'])}",
             f"{icon} Result <b>{net:+.1f}%</b> ({'+' if usd >= 0 else '-'}${abs(usd):,.2f}) · "
             f"best {r['mfe_pct']:+.1f}% · worst {r['mae_pct']:+.1f}% · {r['minutes_to_exit']:.0f} min"]
    good, bad = json.loads(r["positives"]), json.loads(r["negatives"])
    if good:
        lines.append("✅ <b>Went well:</b> " + e(" ".join(good)))
    if bad:
        lines.append("❌ <b>Went wrong:</b> " + e(" ".join(bad)))
    lines.append("💡 <b>Lesson:</b> " + e(r["lesson"]))
    lines.append("<i>Paper trade - nothing was bought or sold.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the learner
# --------------------------------------------------------------------------

def _avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _step(cur: float, target: float, step: float) -> float:
    return round((cur + max(-step, min(step, target - cur))) * 2) / 2


class Learner:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.L = cfg.learning
        self.storage = storage

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        try:
            return json.loads(self.storage.get_value(KEY_STATE) or "{}")
        except ValueError:
            return {}

    @property
    def applies(self) -> bool:
        return self.L.enabled and self.L.mode == "auto"

    def level(self, risk: str) -> StrategyLevel | None:
        """Learned take-profit / stop-loss for a risk level (None = use config)."""
        if not self.applies:
            return None
        lv = self.state().get("levels", {}).get((risk or "HIGH").upper())
        if not lv:
            return None
        return StrategyLevel(take_profit_pct=lv["tp"], stop_loss_pct=lv["sl"])

    def adjustment(self, features: dict) -> tuple[float, list[str]]:
        """Score points for a token that looks like this (negative = caution),
        and the labels of the patterns responsible."""
        if not self.applies:
            return 0.0, []
        pats = self.state().get("patterns", {})
        total, labels = 0.0, []
        for key, label in matching_patterns(features):
            adj = (pats.get(key) or {}).get("adj", 0)
            if adj < 0:
                total += adj
                labels.append(label)
        return max(-self.L.max_score_penalty, total), labels

    def _current_level(self, risk: str, state: dict) -> tuple[float, float]:
        lv = state.get("levels", {}).get(risk)
        if lv:
            return lv["tp"], lv["sl"]
        base = self.cfg.strategy.levels[risk]
        return base.take_profit_pct, base.stop_loss_pct

    # ------------------------------------------------------------------ loop
    def update(self, now: float | None = None, sender=None) -> list[str]:
        """Review newly closed trades; re-analyse everything when due.
        Returns the change descriptions (for logs/tests)."""
        now = now or time.time()
        if not (self.L.enabled and self.cfg.strategy.enabled):
            return []
        self.review_closed(now, sender)
        last = float(self.storage.get_value(KEY_LAST_RETRAIN, "0") or 0)
        if now - last < self.L.retrain_every_hours * 3600:
            return []
        changes = self.retrain(now)
        self.storage.set_value(KEY_LAST_RETRAIN, str(now))
        if changes and sender is not None and self.L.send_updates:
            sender.send(self.changes_message(changes, now), label="LEARNED")
        return [c["text"] for c in changes]

    def review_closed(self, now: float, sender=None) -> int:
        done = {r[0]: (r[1], r[2]) for r in self.storage.conn.execute(
            "SELECT alert_id, final, sent FROM trade_reviews")}
        n = 0
        for t in self.storage.strategy_trades():
            if t["hit"] not in CLOSED or not t["hit_ts"] or not t["price_usd"]:
                continue
            final, sent = done.get(t["id"], (0, 0))
            if final:
                continue
            end = review_end(t)
            obs = self.storage.price_observations(t["id"], t["chain"], t["token_address"], t["ts"], end)
            r = review_trade(t, obs, features_for(self.storage, t))
            self.storage.save_review(t["id"], ts=now, final=int(now >= end), **r)
            n += 1
            if sent:
                continue
            if now - t["hit_ts"] > SEND_REVIEW_IF_CLOSED_WITHIN or not self.L.send_trade_reviews:
                self.storage.mark_review_sent(t["id"])      # old trades: reviewed silently
            elif sender is not None:
                sender.send(review_message(t, r), label="TRADE REVIEW")
                self.storage.mark_review_sent(t["id"])
        self.storage.commit()
        return n

    # ------------------------------------------------------------------ re-analysis
    def _closed_trades(self) -> list:
        return [t for t in self.storage.strategy_trades()
                if t["hit"] in CLOSED and t["hit_ts"] and t["price_usd"] and t["net_return_pct"] is not None]

    def simulate(self, t, obs, tp_pct: float, sl_pct: float) -> float | None:
        """Net % this trade would have made with other exit levels (no runner)."""
        entry = t["price_usd"]
        fake = {"ts": t["ts"], "price_usd": entry, "take_profit_usd": entry * (1 + tp_pct / 100),
                "stop_loss_usd": entry * (1 - sl_pct / 100), "est_cost_pct": t["est_cost_pct"]}
        out = evaluate(fake, obs, t["ts"] + HORIZON + GRACE + 1, self.cfg.trade.round_trip_fee_pct)
        return out["net_return_pct"]

    def retrain(self, now: float) -> list[dict]:
        state = self.state()
        trades = self._closed_trades()
        changes = self._learn_exits(trades, state, now) + self._learn_patterns(trades, state, now)
        state.update(updated=now, trades_used=len(trades), mode=self.L.mode)
        self.storage.set_value(KEY_STATE, json.dumps(state))
        for c in changes:
            self.storage.add_learning_log(now, c["kind"], c["text"], c["why"])
        self.storage.commit()
        for c in changes:
            log.info("Learned: %s (%s)", c["text"], c["why"])
        return changes

    def _learn_exits(self, trades, state: dict, now: float) -> list[dict]:
        L = self.L
        tps = [x for x in TP_GRID if L.tp_min_pct <= x <= L.tp_max_pct]
        sls = [x for x in SL_GRID if L.sl_min_pct <= x <= L.sl_max_pct]
        changes = []
        levels = state.setdefault("levels", {})
        info = state.setdefault("exit_info", {})
        for risk in RISKS:
            paths = []
            for t in trades:
                if (t["risk"] or "HIGH").upper() != risk or now < t["ts"] + HORIZON + GRACE:
                    continue
                obs = self.storage.price_observations(t["id"], t["chain"], t["token_address"],
                                                      t["ts"], t["ts"] + HORIZON + GRACE)
                # Only trades we kept watching for the whole 4 hours can be replayed fairly.
                if obs and obs[-1][0] >= t["ts"] + HORIZON - GRACE:
                    paths.append((t, obs))
            cur_tp, cur_sl = self._current_level(risk, state)
            info[risk] = {"replayable": len(paths), "needed": L.min_trades_for_exits}
            if len(paths) < L.min_trades_for_exits:
                continue

            def score(tp, sl, subset):
                nets = [self.simulate(t, o, tp, sl) for t, o in subset]
                nets = [x for x in nets if x is not None]
                return _avg(nets)

            half = len(paths) // 2
            old, new = paths[:half], paths[half:]
            base = score(cur_tp, cur_sl, paths)
            best = max(((score(tp, sl, paths), tp, sl) for tp in tps for sl in sls), key=lambda x: x[0])
            avg, btp, bsl = best
            info[risk].update(current_avg=round(base, 2), best_avg=round(avg, 2), best_tp=btp, best_sl=bsl)
            if avg - base < MIN_IMPROVEMENT:
                continue
            if score(btp, bsl, old) < score(cur_tp, cur_sl, old) or \
               score(btp, bsl, new) < score(cur_tp, cur_sl, new):
                info[risk]["note"] = "better levels found but not in both older and newer trades - waiting"
                continue
            ntp = min(L.tp_max_pct, max(L.tp_min_pct, _step(cur_tp, btp, L.max_step_pct)))
            nsl = min(L.sl_max_pct, max(L.sl_min_pct, _step(cur_sl, bsl, L.max_step_pct)))
            if (ntp, nsl) == (cur_tp, cur_sl):
                continue
            levels[risk] = {"tp": ntp, "sl": nsl}
            parts = []
            if ntp != cur_tp:
                parts.append(f"take-profit {cur_tp:g}% → {ntp:g}%")
            if nsl != cur_sl:
                parts.append(f"stop-loss {cur_sl:g}% → {nsl:g}%")
            changes.append(dict(kind="exits", text=f"{risk} risk: " + ", ".join(parts),
                                why=f"replaying {len(paths)} past {risk.lower()}-risk trades: the old levels "
                                    f"averaged {base:+.1f}% per trade, +{btp:g}% / -{bsl:g}% would have "
                                    f"averaged {avg:+.1f}% (moving toward it a step at a time)"))
        return changes

    def _learn_patterns(self, trades, state: dict, now: float) -> list[dict]:
        L = self.L
        pats = state.setdefault("patterns", {})
        rows = [(features_for(self.storage, t), t["net_return_pct"]) for t in trades]
        groups: dict[str, list[float]] = {}
        labels: dict[str, str] = {}
        for f, net in rows:
            for key, label in matching_patterns(f):
                groups.setdefault(key, []).append(net)
                labels[key] = label
        all_avg = _avg([n for _, n in rows])
        changes = []
        for key in sorted(set(groups) | set(pats)):
            nets = groups.get(key, [])
            old = (pats.get(key) or {}).get("adj", 0)
            n = len(nets)
            avg = _avg(nets)
            win = sum(1 for x in nets if x > 0) / n * 100 if n else 0
            rest_n = len(rows) - n
            rest_avg = (all_avg * len(rows) - avg * n) / rest_n if rest_n else 0.0
            target = 0.0
            # Judged only against a comparison group: if every trade shares the
            # pattern, it says nothing about which entries are worse.
            if n >= L.min_trades_per_pattern and rest_n >= L.min_trades_per_pattern:
                shrunk = avg * n / (n + SHRINK)
                if shrunk <= -1 and avg - rest_avg <= -2:
                    target = -min(L.max_score_penalty, round(-shrunk * 2))
            new = old + max(-PATTERN_STEP, min(PATTERN_STEP, target - old))
            new = float(round(new))
            pats[key] = {"adj": new, "n": n, "avg": round(avg, 2), "win": round(win), "label": labels.get(
                key, (pats.get(key) or {}).get("label", key))}
            if new != old:
                label = pats[key]["label"]
                if new < old:
                    text = f"More caution: \"{label}\" now needs {-new:.0f} extra score points"
                else:
                    text = (f"Eased: \"{label}\" now needs {-new:.0f} extra score points" if new < 0
                            else f"Lifted: \"{label}\" no longer needs extra score points")
                why = (f"{n} trades, {win:.0f}% won, average {avg:+.1f}% each "
                       f"(other trades {rest_avg:+.1f}%)")
                changes.append(dict(kind="pattern", text=text, why=why))
        return changes

    # ------------------------------------------------------------------ text
    def changes_message(self, changes: list[dict], now: float) -> str:
        e = html.escape
        s = self.cfg.strategy
        n = self.state().get("trades_used", 0)
        head = "🧠 <b>The bot learned something</b>" if self.applies else \
               "🧠 <b>Suggested changes</b> (learning.mode is suggest - nothing was changed)"
        lines = [f"{head} · from {n} closed trades"]
        for c in changes:
            lines.append(f"• <b>{e(c['text'])}</b>\n   <i>Why:</i> {e(c['why'])}")
        lines.append(f"<i>Never changed by learning: safety checks, the {s.daily_loss_limit_pct:g}% daily "
                     f"loss limit, ${s.position_usd:g} trade size, minimum score {s.min_trade_score:g}.</i>")
        return "\n".join(lines)

    def lessons_text(self, now: float) -> str | None:
        """End-of-day: what went wrong / right today and what the bot changed."""
        d0 = day_start(now)
        today = self.storage.reviewed_trades(since=d0)
        if not today:
            return None
        wins = [t for t in today if (t["net_return_pct"] or 0) > 0]
        losses = [t for t in today if (t["net_return_pct"] or 0) <= 0]

        def top(ts, k=3):
            c = Counter(tg for t in ts for tg in json.loads(t["tags"] or "[]") if tg in TAG_TEXT)
            return ", ".join(f"{TAG_TEXT[tg]} ({n})" for tg, n in c.most_common(k)) or "-"

        lines = ["🧠 <b>Today's lessons</b>",
                 f"{len(wins)} wins · {len(losses)} losses"]
        if losses:
            lines.append(f"❌ Losses mostly: {html.escape(top(losses))}")
        if wins:
            lines.append(f"✅ Wins mostly: {html.escape(top(wins))}")
        avg_mfe_loss = _avg([t["mfe_pct"] or 0 for t in losses])
        if losses and avg_mfe_loss > 0:
            lines.append(f"Losing trades were up {avg_mfe_loss:+.1f}% on average at their best.")
        made = [r for r in self.storage.learning_log(50) if r["ts"] >= d0]
        if made:
            lines.append("Changes made today:")
            lines += [f"• {html.escape(r['change'])}" for r in reversed(made)]
        else:
            st = self.state().get("exit_info", {})
            waiting = [f"{k.lower()} {v['replayable']}/{v['needed']}" for k, v in st.items()
                       if v.get("replayable", 0) < v.get("needed", 0)]
            if waiting:
                lines.append("Still collecting trades before changing exits (" + ", ".join(waiting) + ").")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def learning_report(cfg: Config, storage: Storage, out=None, last: int = 10) -> None:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table
    out = out or Console()
    lr = Learner(cfg, storage)
    st = lr.state()
    out.rule("[bold]What the bot has learned[/bold]")
    if not cfg.learning.enabled:
        out.print("Learning is switched off (learning.enabled: false in config.yaml).\n")
        return
    mode = "applied automatically" if lr.applies else "[yellow]suggest only - not applied[/yellow]"
    when = datetime.fromtimestamp(st["updated"]).strftime("%b %d %H:%M") if st.get("updated") else "not yet"
    out.print(f"Mode: {mode} · last re-analysis: {when} · closed trades analysed: {st.get('trades_used', 0)}")

    lv = Table(title="Take-profit / stop-loss per risk level", title_justify="left",
               box=box.SIMPLE_HEAD, header_style="bold cyan")
    for col, j in (("Risk", "left"), ("Your config", "right"), ("Learned", "right"),
                   ("Replayable trades", "right"), ("Avg now → best", "right")):
        lv.add_column(col, justify=j)
    info = st.get("exit_info", {})
    for risk in RISKS:
        base = cfg.strategy.levels[risk]
        learned = st.get("levels", {}).get(risk)
        i = info.get(risk, {})
        lv.add_row(risk, f"+{base.take_profit_pct:g}% / -{base.stop_loss_pct:g}%",
                   f"+{learned['tp']:g}% / -{learned['sl']:g}%" if learned else "-",
                   f"{i.get('replayable', 0)} of {i.get('needed', cfg.learning.min_trades_for_exits)} needed",
                   f"{i['current_avg']:+.1f}% → {i['best_avg']:+.1f}% (+{i['best_tp']:g}/-{i['best_sl']:g})"
                   if "best_avg" in i else "-")
    out.print(lv)

    pats = sorted(((k, v) for k, v in st.get("patterns", {}).items() if v.get("n")),
                  key=lambda kv: (kv[1]["adj"], kv[1]["avg"]))
    if pats:
        pt = Table(title="Entry patterns (negative points = trades like this need a higher score)",
                   title_justify="left", box=box.SIMPLE_HEAD, header_style="bold cyan")
        for col, j in (("Pattern", "left"), ("Trades", "right"), ("Won", "right"),
                       ("Avg result", "right"), ("Score points", "right")):
            pt.add_column(col, justify=j)
        for _, v in pats:
            adj = v["adj"]
            pt.add_row(v["label"], str(v["n"]), f"{v['win']:.0f}%", f"{v['avg']:+.1f}%",
                       f"[red]{adj:+.0f}[/red]" if adj < 0 else "0")
        out.print(pt)
        out.print(f"[dim]A pattern is judged once it has {cfg.learning.min_trades_per_pattern}+ trades.[/dim]")

    reviews = storage.reviewed_trades()
    if reviews:
        losses = [t for t in reviews if (t["net_return_pct"] or 0) <= 0]
        wins = [t for t in reviews if (t["net_return_pct"] or 0) > 0]
        ct = Table(title="Why trades won or lost (all reviews)", title_justify="left",
                   box=box.SIMPLE_HEAD, header_style="bold cyan")
        ct.add_column("Reason")
        ct.add_column("In losses", justify="right")
        ct.add_column("In wins", justify="right")
        cl = Counter(tg for t in losses for tg in json.loads(t["tags"] or "[]"))
        cw = Counter(tg for t in wins for tg in json.loads(t["tags"] or "[]"))
        for tg, _ in (cl + cw).most_common(12):
            if tg in TAG_TEXT:
                ct.add_row(TAG_TEXT[tg], str(cl.get(tg, 0)), str(cw.get(tg, 0)))
        out.print(ct)
        if losses:
            out.print(f"Losing trades were up {_avg([t['mfe_pct'] or 0 for t in losses]):+.1f}% on average "
                      f"at their best; winners dipped {_avg([t['mae_pct'] or 0 for t in wins]):+.1f}% "
                      f"on average first.")

        out.rule(f"[bold]Latest {min(last, len(reviews))} trade reviews[/bold]")
        for t in reviews[-last:][::-1]:
            net = t["net_return_pct"] or 0
            color = "green" if net > 0 else "red"
            when = datetime.fromtimestamp(t["hit_ts"]).strftime("%b %d %H:%M")
            out.print(f"[bold]{t['symbol'] or '?'}[/bold] ({t['chain']}) {when} · "
                      f"{EXIT_NAMES.get(t['hit'], t['hit'])} · [{color}]{net:+.1f}%[/{color}] · "
                      f"best {t['mfe_pct'] or 0:+.1f}% · worst {t['mae_pct'] or 0:+.1f}%", highlight=False)
            for s in json.loads(t["positives"] or "[]"):
                out.print(f"   [green]+[/green] {escape(s)}", highlight=False)
            for s in json.loads(t["negatives"] or "[]"):
                out.print(f"   [red]-[/red] {escape(s)}", highlight=False)
            out.print(f"   Lesson: {t['lesson']}", highlight=False, markup=False)
    else:
        out.print("No trade reviews yet - they appear as strategy trades close.")

    logs = storage.learning_log(10)
    if logs:
        out.rule("[bold]Recent changes[/bold]")
        for r in logs:
            out.print(f"{datetime.fromtimestamp(r['ts']):%b %d %H:%M}  {escape(r['change'])}  "
                      f"[dim]({escape(r['reason'] or '')})[/dim]", highlight=False)
    out.print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="What the bot has learned from its paper trades")
    parser.add_argument("--retrain", action="store_true", help="re-analyse every trade right now")
    parser.add_argument("--last", type=int, default=10, help="how many recent reviews to show")
    parser.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"SETTINGS PROBLEM: {exc}", file=sys.stderr)
        return 2
    if not cfg.storage.database_path.exists():
        print("No data yet - start the scanner first.")
        return 0
    storage = Storage(cfg.storage.database_path)
    try:
        lr = Learner(cfg, storage)
        now = time.time()
        lr.review_closed(now)
        if args.retrain:
            changes = lr.retrain(now)
            storage.set_value(KEY_LAST_RETRAIN, str(now))
            print("Re-analysed every trade: " + (f"{len(changes)} change(s)." if changes else "no changes."))
        learning_report(cfg, storage, last=args.last)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
