"""Your trading rules, applied to a paper account.

Every alert under the strategy is a paper trade from a pretend account
(default $50). Before a trade is suggested, these rules are checked:

  * Minimum score: only tokens whose combined alert score is at least
    min_trade_score (default 83) are traded.
  * Daily loss limit: today's realised losses + the worst case of trades
    still open + this trade's planned loss must stay within N% of the
    balance at midnight. Once losses reach the limit: no new trades today.
  * Position size: a fixed amount, never more than the free cash.
  * Max open trades.
  * High-risk allowance: N high-risk trades a day; each high-risk win earns
    one more for that day. Nothing carries over.
  * No second trade in a token that's already open; after taking profit on
    a token, only re-enter on a strong fresh setup (high score, new high,
    volume spiking again).

Take-profit / stop-loss come from the token's risk rating (LOW / MEDIUM /
HIGH). "Today" and "this week" follow the computer's clock; weeks start on
Monday.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

# Trade outcomes (shared with tracker.py)
HIT_TP, HIT_SL, HIT_EXPIRED, HIT_TRAIL = "TP", "SL", "EXPIRED", "TRAIL"
HIT_OPEN, HIT_RUNNING, HIT_NO_DATA = "OPEN", "RUNNING", "NO_DATA"
CLOSED = (HIT_TP, HIT_SL, HIT_EXPIRED, HIT_TRAIL)
PROFIT_EXITS = (HIT_TP, HIT_TRAIL)


def day_start(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def week_start(now: float) -> float:
    d = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=d.weekday())).timestamp()


def trade_pnl(row) -> float:
    """Dollar result of a closed trade (after fees + slippage)."""
    if row["net_return_pct"] is None:
        return 0.0
    return (row["net_return_pct"] / 100.0) * (row["position_usd"] or 0.0)


def planned_loss(position_usd: float, stop_loss_pct: float, cost_pct: float) -> float:
    """What a trade loses if it stops out normally (gaps can be worse)."""
    return position_usd * (stop_loss_pct + cost_pct) / 100.0


@dataclass
class AccountState:
    balance: float                 # starting balance + all realised results
    day_start_balance: float
    today_pnl: float
    week_start_balance: float
    week_pnl: float
    daily_limit_usd: float
    open_count: int
    open_cost: float               # dollars tied up in open trades
    open_risk: float               # worst normal-case loss of open trades
    high_used_today: int
    high_won_today: int
    high_allowance: int
    open_tokens: set[tuple[str, str]] = field(default_factory=set)

    @property
    def free_cash(self) -> float:
        return max(0.0, self.balance - self.open_cost)

    @property
    def loss_today(self) -> float:
        return max(0.0, -self.today_pnl)

    @property
    def limit_hit(self) -> bool:
        return self.loss_today >= self.daily_limit_usd

    @property
    def week_pct(self) -> float:
        return self.week_pnl / self.week_start_balance * 100 if self.week_start_balance else 0.0

    @property
    def today_pct(self) -> float:
        return self.today_pnl / self.day_start_balance * 100 if self.day_start_balance else 0.0


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    position_usd: float = 0.0
    take_profit_pct: float = 0.0
    stop_loss_pct: float = 0.0


class Strategy:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.s = cfg.strategy
        self.storage = storage

    def level(self, risk: str):
        return self.s.levels.get((risk or "HIGH").upper(), self.s.levels["HIGH"])

    # ------------------------------------------------------------------ account
    def state(self, now: float | None = None) -> AccountState:
        now = now or time.time()
        trades = self.storage.strategy_trades()
        d0, w0 = day_start(now), week_start(now)
        start = self.s.starting_balance_usd
        realised = before_day = before_week = today = week = 0.0
        open_count = 0
        open_cost = open_risk = 0.0
        high_used = high_won = 0
        open_tokens: set[tuple[str, str]] = set()
        for t in trades:
            closed = t["hit"] in CLOSED and t["hit_ts"]
            if closed:
                pnl = trade_pnl(t)
                realised += pnl
                if t["hit_ts"] < d0:
                    before_day += pnl
                else:
                    today += pnl
                if t["hit_ts"] < w0:
                    before_week += pnl
                else:
                    week += pnl
                if (t["risk"] or "").upper() == "HIGH" and t["hit_ts"] >= d0 and pnl > 0:
                    high_won += 1
            elif t["hit"] != HIT_NO_DATA:
                open_count += 1
                open_cost += t["position_usd"] or 0.0
                open_tokens.add((t["chain"], t["token_address"]))
                if t["hit"] != HIT_RUNNING:          # runners have their profit locked in
                    sl_pct = (1 - (t["stop_loss_usd"] or 0) / t["price_usd"]) * 100 if t["price_usd"] else 0
                    open_risk += planned_loss(t["position_usd"] or 0, sl_pct, t["est_cost_pct"] or 0)
            if (t["risk"] or "").upper() == "HIGH" and t["ts"] >= d0:
                high_used += 1
        day_bal = start + before_day
        allowance = self.s.high_risk_per_day - high_used
        if self.s.high_risk_win_earns_extra:
            allowance += high_won
        return AccountState(
            balance=start + realised, day_start_balance=day_bal, today_pnl=today,
            week_start_balance=start + before_week, week_pnl=week,
            daily_limit_usd=day_bal * self.s.daily_loss_limit_pct / 100,
            open_count=open_count, open_cost=open_cost, open_risk=open_risk,
            high_used_today=high_used, high_won_today=high_won,
            high_allowance=max(0, allowance), open_tokens=open_tokens)

    # ------------------------------------------------------------------ rules
    def decide(self, row, state: AccountState, cost_pct_for, now: float | None = None) -> Decision:
        """Should this scan row become a paper trade? cost_pct_for(size) -> est. cost %."""
        risk = (row.composite.risk if row.composite else "HIGH").upper()
        lvl = self.level(risk)
        key = (row.snap.chain, row.snap.token_address)

        score = row.composite.score if row.composite else 0
        if score < self.s.min_trade_score:
            return Decision(False, f"score {score:.0f} is below the {self.s.min_trade_score:.0f} minimum for trades")
        if state.limit_hit:
            return Decision(False, "daily loss limit reached")
        if key in state.open_tokens:
            return Decision(False, "already holding this token")
        if state.open_count >= self.s.max_open_trades:
            return Decision(False, f"{self.s.max_open_trades} trades already open")
        size = min(self.s.position_usd, state.free_cash)
        if size < max(self.s.min_position_usd, 0.01):
            return Decision(False, "not enough free cash in the paper account")
        if risk == "HIGH" and state.high_allowance <= 0:
            return Decision(False, "no high-risk trades left today")
        risk_now = state.loss_today + state.open_risk + planned_loss(size, lvl.stop_loss_pct, cost_pct_for(size))
        if risk_now > state.daily_limit_usd:
            return Decision(False, f"could break the daily loss limit (${state.daily_limit_usd:.2f})")
        why = self._reentry_block(row, key)
        if why:
            return Decision(False, why)
        return Decision(True, "", size, lvl.take_profit_pct, lvl.stop_loss_pct)

    def _reentry_block(self, row, key) -> str:
        last = self.storage.last_strategy_trade(*key)
        if last is None or last["hit"] not in PROFIT_EXITS:
            return ""
        s = self.s
        score = row.composite.score if row.composite else 0
        if score < s.reentry_min_score:
            return f"re-entry needs score {s.reentry_min_score:.0f}+ after taking profit"
        if s.reentry_require_new_high and last["price_usd"] and last["gross_return_pct"] is not None:
            exit_price = last["price_usd"] * (1 + last["gross_return_pct"] / 100)
            if (row.snap.price_usd or 0) <= exit_price:
                return "re-entry needs a new high above the last profit exit"
        if s.reentry_require_volume_spike and "volume_spike" not in row.momentum.signals:
            return "re-entry needs volume spiking again"
        return ""

    @staticmethod
    def register(state: AccountState, row, decision: Decision, cost_pct: float) -> None:
        """Update the in-memory state after opening a trade (same scan)."""
        state.open_count += 1
        state.open_cost += decision.position_usd
        state.open_risk += planned_loss(decision.position_usd, decision.stop_loss_pct, cost_pct)
        state.open_tokens.add((row.snap.chain, row.snap.token_address))
        if (row.composite.risk or "").upper() == "HIGH":
            state.high_used_today += 1
            state.high_allowance = max(0, state.high_allowance - 1)

    # ------------------------------------------------------------------ text
    def status_line(self, state: AccountState) -> str:
        s = self.s
        return (f"Paper account ${state.balance:,.2f} (started ${s.starting_balance_usd:,.0f}) · "
                f"today {'+' if state.today_pnl >= 0 else '-'}${abs(state.today_pnl):,.2f} "
                f"(loss limit ${state.daily_limit_usd:,.2f}) · "
                f"week {state.week_pct:+.1f}% (goal {s.weekly_target_min_pct:g}-{s.weekly_target_max_pct:g}%) · "
                f"open {state.open_count}/{s.max_open_trades} · high-risk left {state.high_allowance}")
