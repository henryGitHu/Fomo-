"""Self-learning: trade reviews, exit tuning, entry-pattern caution, guardrails."""
from __future__ import annotations

import json
import time

import pytest
from rich.console import Console

from scanner import learning
from scanner.config import ConfigError, load_config
from scanner.learning import KEY_STATE, Learner, features_from_row, review_trade
from scanner.storage import Storage
from scanner.strategy import HIT_SL, HIT_TP, Strategy
from scanner.summaries import Summaries
from scanner.tracker import GRACE, HORIZON, Tracker

from test_phase1 import ROOT
from test_phase3 import CHAT, TOKEN, hot_snap, make_row
from test_strategy import Sent, noon

H = 3600


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml", env_path=ROOT / "none.env")
    c.secrets = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    assert c.learning.enabled and c.learning.mode == "auto"
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


def sender(cfg, sent):
    from scanner.alerts import AlertSender
    return AlertSender(cfg, sent.http(), dry_run=False)


def trade(storage, ts, addr="Tok", risk="MEDIUM", tp=15, sl=7, path=(), features=None, price=1.0):
    """A strategy trade with recorded prices: path = [(minutes, price), ...]."""
    aid = storage.add_alert(ts=ts, chain="Solana", token_address=addr, symbol=addr[:6], score=80,
                            signals="[]", safety_status="PASS", price_usd=price,
                            take_profit_usd=price * (1 + tp / 100), stop_loss_usd=price * (1 - sl / 100),
                            position_usd=10, est_cost_pct=1.0, risk=risk, dry_run=0, strategy_trade=1,
                            features=json.dumps(features) if features is not None else None)
    for m, p in path:
        storage.add_alert_price(aid, ts + m * 60, p * price)
    storage.commit()
    return aid


def row_of(storage, aid):
    return next(t for t in storage.strategy_trades() if t["id"] == aid)


# ------------------------------------------------------------------ one trade's review

def test_stop_too_tight_review():
    t = {"ts": 0, "price_usd": 1.0, "take_profit_usd": 1.15, "stop_loss_usd": 0.93, "hit": HIT_SL,
         "hit_ts": 600, "gross_return_pct": -7.5, "net_return_pct": -8.5}
    obs = [(300, 1.10), (600, 0.925), (3600, 1.20)]          # up 10%, stopped, then +20%
    r = review_trade(t, obs, {"pc_h1": 55, "buy_ratio_m5": 0.5, "safety": "PASS"})
    tags = json.loads(r["tags"])
    for tag in ("stop_too_tight", "almost_won", "late_entry", "weak_buyers", "fast_dump"):
        assert tag in tags, tag
    assert r["mfe_pct"] == pytest.approx(10) and r["after_high_pct"] == pytest.approx(20)
    assert "wider stops" in r["lesson"]
    bad = " ".join(json.loads(r["negatives"]))
    assert "climbed to +20%" in bad and "already up 55%" in bad


def test_quick_win_that_kept_running():
    t = {"ts": 0, "price_usd": 1.0, "take_profit_usd": 1.08, "stop_loss_usd": 0.95, "hit": HIT_TP,
         "hit_ts": 900, "gross_return_pct": 8.0, "net_return_pct": 7.0}
    r = review_trade(t, [(600, 1.03), (900, 1.09), (5400, 1.40)], {"buy_ratio_m5": 0.8})
    tags = json.loads(r["tags"])
    assert "quick_win" in tags and "left_on_table" in tags
    assert "runner rule" in r["lesson"]
    assert any("Buyers were 80%" in s for s in json.loads(r["positives"]))


def test_stop_that_saved_us():
    t = {"ts": 0, "price_usd": 1.0, "take_profit_usd": 1.15, "stop_loss_usd": 0.93, "hit": HIT_SL,
         "hit_ts": 1800, "gross_return_pct": -7.2, "net_return_pct": -8.2}
    r = review_trade(t, [(900, 0.99), (1800, 0.928), (4000, 0.5)], {})
    assert "stop_saved" in json.loads(r["tags"])
    assert any("fell to -50%" in s for s in json.loads(r["positives"]))


def test_reviews_saved_and_sent_once(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "l.db"), Sent()
    now = time.time()
    recent = trade(storage, now - 1200, addr="Recent", path=[(5, 0.99), (10, 0.92)])
    old = trade(storage, now - 30 * H, addr="Old", path=[(5, 0.99), (10, 0.92)])
    Tracker(cfg, storage).update(now)
    lr = Learner(cfg, storage)
    assert lr.review_closed(now, sender(cfg, sent)) == 2
    assert len(sent.texts) == 1 and "Trade review: Recent" in sent.texts[0]
    assert "Went wrong" in sent.texts[0] and "Lesson" in sent.texts[0]
    assert storage.review(old)["final"] == 1 and storage.review(recent)["final"] == 0
    lr.review_closed(now + 60, sender(cfg, sent))                 # updated again, not re-sent
    assert len(sent.texts) == 1


def test_closed_trades_keep_being_watched(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    trade(storage, now - 1200, path=[(10, 0.92)])
    Tracker(cfg, storage).update(now)
    a = storage.alerts_to_track(now - 13 * H)[0]
    assert a["cur_hit"] == HIT_SL
    t = Tracker(cfg, storage)
    assert t._needs_prices(a, now)                                # inside the 4h window
    assert not t._needs_prices(a, a["ts"] + HORIZON + GRACE + 60)
    cfg.learning.enabled = False
    assert not t._needs_prices(a, now)


# ------------------------------------------------------------------ exits

def seed_noisy_medium(storage, now, n=24):
    """MEDIUM trades that dip -7.5% (just through the 7% stop), then rally to +13%."""
    for i in range(n):
        trade(storage, now - 6 * H - i * 900, addr=f"Noisy{i}",
              path=[(5, 0.97), (10, 0.925), (30, 1.05), (60, 1.13), (200, 1.02), (240, 1.0)])


def test_learns_wider_stop_and_closer_target(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "l.db"), Sent()
    now = time.time()
    seed_noisy_medium(storage, now)
    Tracker(cfg, storage).update(now)
    assert all(t["hit"] == HIT_SL for t in storage.strategy_trades())
    lr = Learner(cfg, storage)
    changes = lr.update(now, sender(cfg, sent))
    assert any(c.startswith("MEDIUM risk:") for c in changes)
    lv = Strategy(cfg, storage).level("MEDIUM")
    assert lv.stop_loss_pct > 7 and lv.take_profit_pct < 15          # moved toward what worked
    assert lv.stop_loss_pct - 7 <= cfg.learning.max_step_pct           # but only one step
    assert 15 - lv.take_profit_pct <= cfg.learning.max_step_pct
    assert Strategy(cfg, storage).level("LOW").take_profit_pct == 8   # too few LOW trades: unchanged
    msg = sent.texts[-1]
    assert "The bot learned something" in msg and "Why:" in msg and "minimum score 78" in msg
    assert lr.update(now + 60) == []                                   # not due again yet
    assert storage.learning_log()[0]["kind"] in ("exits", "pattern")


def test_needs_enough_trades_before_changing_exits(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_noisy_medium(storage, now, n=cfg.learning.min_trades_for_exits - 1)
    Tracker(cfg, storage).update(now)
    Learner(cfg, storage).retrain(now)
    assert Strategy(cfg, storage).level("MEDIUM").take_profit_pct == 15
    info = Learner(cfg, storage).state()["exit_info"]["MEDIUM"]
    assert info["replayable"] == cfg.learning.min_trades_for_exits - 1


def test_learned_levels_stay_in_bounds(cfg, tmp_path):
    cfg.learning.max_step_pct = 20
    cfg.learning.sl_max_pct = 7.5
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_noisy_medium(storage, now)
    Tracker(cfg, storage).update(now)
    Learner(cfg, storage).retrain(now)
    lv = Strategy(cfg, storage).level("MEDIUM")
    assert cfg.learning.tp_min_pct <= lv.take_profit_pct <= cfg.learning.tp_max_pct
    assert lv.stop_loss_pct <= 7.5


def test_suggest_mode_changes_nothing(cfg, tmp_path):
    cfg.learning.mode = "suggest"
    storage, sent = Storage(tmp_path / "l.db"), Sent()
    now = time.time()
    seed_noisy_medium(storage, now)
    Tracker(cfg, storage).update(now)
    assert Learner(cfg, storage).update(now, sender(cfg, sent))
    assert Strategy(cfg, storage).level("MEDIUM").take_profit_pct == 15
    assert "nothing was changed" in sent.texts[-1]


# ------------------------------------------------------------------ entry patterns

def seed_patterns(storage, now, n=15):
    """Late entries (up 50% in the hour) lose; calm entries win."""
    for i in range(n):
        for name, pc_h1, hit, net in (("Late", 50, HIT_SL, -8.0), ("Calm", 5, HIT_TP, 7.0)):
            aid = trade(storage, now - 20 * H - i * 600, addr=f"{name}{i}",
                        features={"pc_h1": pc_h1, "risk": "LOW", "chain": "Solana", "score": 80})
            storage.save_outcome(aid, hit=hit, hit_ts=now - 19 * H - i * 600,
                                 gross_return_pct=net + 1, net_return_pct=net, updated_ts=now)
    storage.commit()


def test_losing_pattern_needs_more_score(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_patterns(storage, now)
    changes = Learner(cfg, storage).retrain(now)
    assert any('"already up 40%+ in the last hour"' in c["text"] for c in changes)
    pats = json.loads(storage.get_value(KEY_STATE))["patterns"]
    assert pats["pump_h1_40"]["adj"] == -5                     # one step at a time
    assert pats["calm_h1"]["adj"] == 0                          # winners are never penalised

    strat = Strategy(cfg, storage)
    st = strat.state(noon())
    late = make_row(cfg, snap=hot_snap(token_address="NewLate", price_change_h1=60))
    late.composite.score = 80
    d = strat.decide(late, st, lambda s: 1.0, noon())
    assert not d.ok and "learned caution" in d.reason and "already up 40%" in d.reason
    late.composite.score = 90                                   # strong enough to overcome it
    d = strat.decide(late, st, lambda s: 1.0, noon())
    assert d.ok and d.learn_adjust == -5 and "passed learned caution" in d.learn_note
    calm = make_row(cfg, snap=hot_snap(token_address="NewCalm", price_change_h1=5))
    calm.composite.score = 80
    assert strat.decide(calm, st, lambda s: 1.0, noon()).ok


def test_penalty_grows_gradually_and_is_capped(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_patterns(storage, now, n=40)
    lr = Learner(cfg, storage)
    for k in range(8):
        lr.retrain(now + k)
    adj = json.loads(storage.get_value(KEY_STATE))["patterns"]["pump_h1_40"]["adj"]
    assert -cfg.learning.max_score_penalty <= adj < -5
    row = make_row(cfg, snap=hot_snap(price_change_h1=60))
    total, _ = lr.adjustment(features_from_row(row, now))
    assert total >= -cfg.learning.max_score_penalty


def test_min_trade_score_is_never_lowered(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_patterns(storage, now)
    Learner(cfg, storage).retrain(now)
    strat = Strategy(cfg, storage)
    calm = make_row(cfg, snap=hot_snap(token_address="Low", price_change_h1=5))
    calm.composite.score = 77
    d = strat.decide(calm, strat.state(noon()), lambda s: 1.0, noon())
    assert not d.ok and "below the 78 minimum" in d.reason


def test_alert_stores_features_and_shows_learning(cfg, tmp_path):
    from test_strategy import manager
    storage, sent = Storage(tmp_path / "l.db"), Sent()
    now = time.time()
    seed_patterns(storage, now)
    Learner(cfg, storage).retrain(now)
    row = make_row(cfg, snap=hot_snap(token_address="Fresh", price_change_h1=60))
    row.composite.score = 95
    ids = manager(cfg, storage, sent).process([row], noon())
    a = storage.conn.execute("SELECT features, learn_adjust FROM alerts WHERE id = ?", (ids[0],)).fetchone()
    assert json.loads(a["features"])["pc_h1"] == 60 and a["learn_adjust"] == -5
    assert "🧠" in sent.texts[-1]


# ------------------------------------------------------------------ summaries, report, config

def test_end_of_day_lessons(cfg, tmp_path):
    storage, sent = Storage(tmp_path / "l.db"), Sent()
    now = noon()
    trade(storage, now - 2 * H, addr="Loser", path=[(5, 1.10), (10, 0.92), (60, 1.2)])
    Tracker(cfg, storage).update(now)
    Learner(cfg, storage).review_closed(now)
    s = Summaries(cfg, storage, sender(cfg, sent))
    text = s.lessons(now)
    assert "Today's lessons" in text and "0 wins · 1 losses" in text
    assert "stopped out, then recovered" in text


def test_learning_report_and_cli(cfg, tmp_path):
    storage = Storage(tmp_path / "l.db")
    now = time.time()
    seed_noisy_medium(storage, now)
    Tracker(cfg, storage).update(now)
    lr = Learner(cfg, storage)
    lr.review_closed(now)
    lr.retrain(now)
    out = Console(record=True, width=160)
    learning.learning_report(cfg, storage, out)
    text = out.export_text()
    for part in ("What the bot has learned", "Take-profit / stop-loss per risk level",
                 "Why trades won or lost", "stopped out, then recovered", "trade reviews",
                 "Recent changes", "MEDIUM"):
        assert part in text, part
    storage.close()
    db = (tmp_path / "l.db").as_posix()
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text((ROOT / "config.yaml").read_text().replace("data/scanner.db", db))
    assert learning.main(["--config", str(cfgfile), "--retrain"]) == 0


def test_learning_section_optional_and_validated(tmp_path):
    text = (ROOT / "config.yaml").read_text()
    start = text.index("learning:\n")
    end = text.index("# ----", start)
    p = tmp_path / "config.yaml"
    p.write_text(text[:start] + text[end:])
    cfg = load_config(p, env_path=tmp_path / "none.env")
    assert cfg.learning.enabled and cfg.learning.min_trades_for_exits == 20
    p.write_text(text.replace("  mode: auto", "  mode: yolo"))
    with pytest.raises(ConfigError, match="learning.mode"):
        load_config(p, env_path=tmp_path / "none.env")
