"""Your strategy's rules: paper account, daily loss limit, sizing, risk levels,
high-risk allowance, re-entry, runners, and the reports."""
from __future__ import annotations

import json
import time
from datetime import datetime

import httpx
import pytest
from rich.console import Console

from scanner import report
from scanner.alerts import AlertManager
from scanner.config import load_config
from scanner.http import HttpClient
from scanner.models import Snapshot
from scanner.storage import Storage
from scanner.strategy import HIT_RUNNING, HIT_SL, HIT_TP, HIT_TRAIL, Strategy
from scanner.summaries import Summaries
from scanner.tracker import Tracker, evaluate

from test_phase1 import ROOT
from test_phase3 import CHAT, TOKEN, hot_snap, make_row, passed

STATUS_UNVERIFIED = "UNVERIFIED"


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml", env_path=ROOT / "none.env")
    c.secrets = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    assert c.strategy.enabled                       # on by default
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


def noon():
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


class Sent:
    def __init__(self):
        self.texts = []

    def http(self):
        def handler(request):
            self.texts.append(json.loads(request.content).get("text", ""))
            return httpx.Response(200, json={"ok": True})
        return HttpClient(max_retries=0, transport=httpx.MockTransport(handler))


def manager(cfg, storage, sent):
    return AlertManager(cfg, storage, sent.http(), dry_run=False)


def seed(storage, ts, hit, net, risk="LOW", size=10.0, addr="Seed", price=1.0, gross=None, closed_ts=None):
    aid = storage.add_alert(ts=ts, chain="Solana", token_address=addr, symbol=addr[:6], score=75,
                            signals="[]", safety_status="PASS", price_usd=price,
                            take_profit_usd=price * 1.08, stop_loss_usd=price * 0.95,
                            position_usd=size, est_cost_pct=1.2, risk=risk, dry_run=0, strategy_trade=1)
    if hit is not None:
        storage.save_outcome(aid, hit=hit, hit_ts=closed_ts or ts + 600,
                             gross_return_pct=gross if gross is not None else net + 1.2,
                             net_return_pct=net, updated_ts=ts)
    storage.commit()
    return aid


def high_row(cfg, **kw):
    snap = hot_snap(liquidity_usd=40_000, **kw)          # thin liquidity -> HIGH risk
    row = make_row(cfg, snap=snap)
    assert row.composite.risk == "HIGH"
    return row


# ------------------------------------------------------------------ sizing + levels

def test_trade_uses_strategy_size_and_risk_levels(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "s.db"), Sent()
    m = manager(cfg, storage, sent)
    row = make_row(cfg)                                   # LOW risk token
    assert row.composite.risk == "LOW"
    ids = m.process([row], noon())
    a = storage.conn.execute("SELECT * FROM alerts WHERE id = ?", (ids[0],)).fetchone()
    assert a["strategy_trade"] == 1 and a["position_usd"] == 10
    assert a["take_profit_usd"] == pytest.approx(0.01 * 1.08)      # LOW: +8%
    assert a["stop_loss_usd"] == pytest.approx(0.01 * 0.95)        # LOW: -5%
    text = sent.texts[0]
    assert "+8%" in text and "-5%" in text and "Paper account $50.00" in text and "trailing stop" in text


def test_high_risk_levels(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    manager(cfg, storage, Sent()).process([high_row(cfg)], noon())
    a = storage.conn.execute("SELECT * FROM alerts").fetchone()
    assert a["take_profit_usd"] == pytest.approx(0.01 * 1.25) and a["stop_loss_usd"] == pytest.approx(0.01 * 0.90)


# ------------------------------------------------------------------ daily loss limit

def test_daily_limit_stops_trading_and_notifies_once(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "s.db"), Sent()
    now = noon()
    seed(storage, now - 3600, HIT_SL, -80.0, addr="L1")          # -$8.00 on $10 > $7.50 limit
    m = manager(cfg, storage, sent)
    assert m.process([make_row(cfg)], now) == []
    assert any("Daily loss limit reached" in t for t in sent.texts)
    n = len(sent.texts)
    assert m.process([make_row(cfg)], now + 600) == [] and len(sent.texts) == n   # only one notice


def test_limit_counts_open_trade_risk(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 3600, HIT_SL, -60.0, addr="L1")          # -$6.00 lost today
    st = Strategy(cfg, storage).state(now)
    assert not st.limit_hit and st.daily_limit_usd == pytest.approx(50 * 0.15)
    # A new LOW trade risks $10 x (5% + ~1.1%) = ~$0.61 -> 6.61 < 7.50: allowed.
    assert manager(cfg, storage, Sent()).process([make_row(cfg)], now)
    # Two more open trades push the worst case past the limit.
    ids = manager(cfg, storage, Sent()).process(
        [make_row(cfg, snap=hot_snap(token_address=f"T{i}")) for i in range(5)], now)
    assert len(ids) == 1                                          # 6.0 + 0.61*3 > 7.5 after the 2nd


def test_limit_is_based_on_start_of_day_balance(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 2 * 86400, HIT_TP, 100.0, size=50, addr="Old")   # +$50 two days ago
    st = Strategy(cfg, storage).state(now)
    assert st.balance == pytest.approx(100) and st.daily_limit_usd == pytest.approx(15)


# ------------------------------------------------------------------ other rules

def test_max_open_trades_and_same_token(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    cfg.alerts.max_alerts_per_scan = 10
    m = manager(cfg, storage, Sent())
    rows = [make_row(cfg, snap=hot_snap(token_address=f"T{i}")) for i in range(7)]
    assert len(m.process(rows, noon())) == 5                      # max 5 open, $10 each = $50
    st = Strategy(cfg, storage).state(noon())
    assert st.open_count == 5 and st.free_cash == pytest.approx(0)
    assert m.process([make_row(cfg, snap=hot_snap(token_address="T0"))], noon() + 7200) == []


def test_high_risk_allowance_and_refund(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    for i in range(5):                                            # 5 high-risk trades today
        seed(storage, now - 3600 + i, HIT_SL if i else HIT_TP, -5.0 if i else 10.0, risk="HIGH",
             addr=f"H{i}", closed_ts=now - 1800)
    st = Strategy(cfg, storage).state(now)
    assert st.high_used_today == 5 and st.high_won_today == 1 and st.high_allowance == 1
    m = manager(cfg, storage, Sent())
    assert len(m.process([high_row(cfg, token_address="NewH1")], now)) == 1   # the earned one
    assert m.process([high_row(cfg, token_address="NewH2")], now + 3600) == []  # none left
    # Low/medium-risk trades are unaffected.
    assert m.process([make_row(cfg, snap=hot_snap(token_address="Low1"))], now + 3600)


def test_high_risk_allowance_resets_daily(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    for i in range(5):
        seed(storage, now - 86400 + i, HIT_SL, -5.0, risk="HIGH", addr=f"H{i}")
    assert Strategy(cfg, storage).state(now).high_allowance == 5


def test_reentry_after_profit_needs_strong_fresh_setup(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 7200, HIT_TP, 6.8, addr="Mint111", price=0.01, gross=8.0)   # exited at 0.0108
    m = manager(cfg, storage, Sent())
    weak = make_row(cfg, snap=hot_snap(price_usd=0.012))
    weak.composite.score = 70
    assert m.process([weak], now) == []                          # score below 80
    lower = make_row(cfg, snap=hot_snap(price_usd=0.0105))
    lower.composite.score = 90
    assert m.process([lower], now) == []                         # not a new high
    strong = make_row(cfg, snap=hot_snap(price_usd=0.012))
    strong.composite.score = 90
    assert "volume_spike" in strong.momentum.signals
    assert m.process([strong], now)                              # all conditions met


def test_reentry_after_stop_loss_uses_normal_rules(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 7200, HIT_SL, -6.2, addr="Mint111", price=0.01)
    row = make_row(cfg)
    row.composite.score = 85                                    # above 78, below the 90 re-entry bar
    assert manager(cfg, storage, Sent()).process([row], now)


# ------------------------------------------------------------------ runners

T0 = 1_700_000_000.0


def runner_alert(state=1):
    return {"ts": T0, "price_usd": 1.0, "take_profit_usd": 1.15, "stop_loss_usd": 0.93,
            "est_cost_pct": 1.0, "runner_state": state, "trail_pct": 8.0, "runner_hold_hours": 12.0}


def readings(*pairs):
    return [(T0 + m * 60, p) for m, p in pairs]


def test_runner_trails_past_target():
    obs = readings((5, 1.10), (10, 1.16), (20, 1.40), (30, 1.60), (40, 1.45))
    out = evaluate(runner_alert(), obs, T0 + 3600, 1.0)
    assert out["hit"] == HIT_TRAIL and out["gross_return_pct"] == pytest.approx(45.0)   # 1.60 peak, -8% -> sold 1.45


def test_runner_never_gives_back_the_target():
    obs = readings((10, 1.16), (20, 1.18), (30, 1.12))           # falls below +15% target
    out = evaluate(runner_alert(), obs, T0 + 3600, 1.0)
    assert out["hit"] == HIT_TRAIL and out["gross_return_pct"] == pytest.approx(12.0)   # sold at price seen


def test_runner_still_running_then_time_limit():
    obs = readings((10, 1.20), (100, 1.30))
    assert evaluate(runner_alert(), obs, T0 + 3 * 3600, 1.0)["hit"] == HIT_RUNNING
    out = evaluate(runner_alert(), obs + readings((700, 1.35)), T0 + 13 * 3600, 1.0)
    assert out["hit"] == HIT_TRAIL and out["gross_return_pct"] == pytest.approx(35.0)


def test_non_runner_takes_profit_normally():
    out = evaluate(runner_alert(state=0), readings((10, 1.20)), T0 + 3600, 1.0)
    assert out["hit"] == HIT_TP and out["gross_return_pct"] == pytest.approx(15.0)


def tracker_with_trade(cfg, tmp_path, m5, buys, sells):
    storage = Storage(tmp_path / "t.db")
    now = time.time()
    aid = seed(storage, now - 600, None, 0, price=1.0, addr="Run1")
    storage.conn.execute("UPDATE alerts SET take_profit_usd = 1.08 WHERE id = ?", (aid,))
    storage.commit()
    snap = Snapshot(ts=now - 30, chain="Solana", token_address="Run1", symbol="RUN", name="Run",
                    pair_address="P", dex="d", url="", source="dexscreener", price_usd=1.10,
                    price_change_m5=m5, price_change_h1=25, volume_m5=60000, volume_h1=200000,
                    buys_m5=buys, sells_m5=sells, buys_h1=2000, sells_h1=900, liquidity_usd=400000)
    storage.add_snapshots([snap])
    storage.commit()
    Tracker(cfg, storage).update(now)
    return storage.conn.execute("SELECT runner_state FROM alerts WHERE id = ?", (aid,)).fetchone()[0]


def test_strong_momentum_at_target_becomes_runner(cfg, tmp_path):
    assert tracker_with_trade(cfg, tmp_path, m5=6, buys=300, sells=80) == 1


def test_weak_momentum_at_target_takes_profit(cfg, tmp_path):
    assert tracker_with_trade(cfg, tmp_path, m5=-2, buys=50, sells=150) == 0


# ------------------------------------------------------------------ reporting

def test_account_state_and_weekly_numbers(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 3600, HIT_TP, 13.8, addr="A")             # +$1.38 today
    seed(storage, now - 1800, None, 0, addr="B")                   # still open
    st = Strategy(cfg, storage).state(now)
    assert st.balance == pytest.approx(51.38) and st.today_pnl == pytest.approx(1.38)
    assert st.open_count == 1 and st.week_pct > 0


def test_report_account_section(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    now = noon()
    seed(storage, now - 3600, HIT_TP, 13.8, addr="A")
    seed(storage, now - 3000, HIT_SL, -6.2, risk="HIGH", addr="B")
    seed(storage, now - 2000, HIT_TRAIL, 40.0, addr="C")
    out = Console(record=True, width=110)
    report.account_report(cfg, storage, out, now)
    text = out.export_text()
    for needle in ("Your strategy - paper account", "Balance now", "$54.76", "Week by week",
                   "High-risk trades", "Runners", "Days the daily loss limit was reached"):
        assert needle in text, needle


def test_summaries_show_account(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "s.db"), Sent()
    from scanner.alerts import AlertSender
    s = Summaries(cfg, storage, AlertSender(cfg, sent.http(), False))
    lines = "\n".join(s.account_lines(noon()))
    assert "Paper account $50.00" in lines and "goal 5-10%" in lines and "loss limit used $0.00 of $7.50" in lines


# ------------------------------------------------------------------ minimum trade score

def test_only_scores_78_or_better_are_traded(cfg, tmp_path):
    storage = Storage(tmp_path / "s.db")
    m = manager(cfg, storage, Sent())
    below = make_row(cfg, snap=hot_snap(token_address="Below"))
    below.composite.score = 77.9
    at = make_row(cfg, snap=hot_snap(token_address="At"))
    at.composite.score = 78
    ids = m.process([below, at], noon())
    traded = [r["token_address"] for r in storage.strategy_trades()]
    assert len(ids) == 1 and traded == ["At"]
    d = Strategy(cfg, storage).decide(below, Strategy(cfg, storage).state(noon()), lambda s: 1.0)
    assert not d.ok and "below the 78 minimum" in d.reason


def test_reentry_bar_is_stricter_than_entry(cfg, tmp_path):
    assert cfg.strategy.reentry_min_score > cfg.strategy.min_trade_score
    storage = Storage(tmp_path / "s.db")
    seed(storage, noon() - 7200, HIT_TP, 6.8, addr="Mint111", price=0.01, gross=8.0)
    row = make_row(cfg, snap=hot_snap(price_usd=0.012))
    row.composite.score = 86                                    # good enough to enter, not to re-enter
    assert manager(cfg, storage, Sent()).process([row], noon()) == []


def test_digest_mentions_trade_minimum(cfg, tmp_path):
    from scanner.alerts import AlertSender
    s = Summaries(cfg, Storage(tmp_path / "s.db"), AlertSender(cfg, Sent().http(), False))
    assert "Trades need a score of 78+" in s.digest_text([], noon())
