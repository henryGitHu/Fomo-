"""Phase 3 tests: composite score, trade suggestion, alert rules and delivery."""
from __future__ import annotations

import copy
import json
import sqlite3
import time

import httpx
import pytest

from scanner.alerts import AlertManager, AlertSender, estimate_slippage_pct, format_alert, suggest_trade, to_plain
from scanner.config import load_config
from scanner.http import HttpClient
from scanner.main import Scanner
from scanner.models import ScanRow, Snapshot
from scanner.safety import FAIL, PASS, UNKNOWN, STATUS_FAIL, STATUS_PASS, STATUS_UNVERIFIED, SafetyCheck, SafetyResult
from scanner.scoring import FilterResult, composite_score, momentum_score, safety_margin
from scanner.storage import Storage

from test_phase1 import ROOT, fake_transport, fast
from test_phase2 import GOOD_RUGCHECK

TOKEN = "123456:TESTTOKEN"
CHAT = "987654321"


@pytest.fixture
def cfg():
    c = fast(load_config(ROOT / "config.yaml", env_path=ROOT / "none.env"))
    c.secrets = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


def hot_snap(**kw):
    now = time.time()
    base = dict(ts=now, chain="Solana", token_address="Mint111", symbol="HOT", name="Hot <Token>",
                pair_address="Pool1", dex="raydium", url="https://dexscreener.com/solana/pool1",
                source="dexscreener", price_usd=0.01, price_change_m5=6, price_change_h1=25,
                volume_m5=60000, volume_h1=200000, buys_m5=300, sells_m5=80, buys_h1=2000,
                sells_h1=900, liquidity_usd=400000, market_cap_usd=5e6, pair_created_at=now - 3 * 86400)
    base.update(kw)
    return Snapshot(**base)


def passed(now=None, top10=10.0, lp=100.0, status=STATUS_PASS):
    checks = [SafetyCheck("mint_authority", PASS, "Mint authority revoked"),
              SafetyCheck("top10_holders", PASS, f"Top 10 wallets hold {top10:.0f}%", True, top10),
              SafetyCheck("lp_locked", PASS, f"{lp:.0f}% of LP burned/locked", True, lp)]
    if status == STATUS_UNVERIFIED:
        checks.append(SafetyCheck("freeze_authority", UNKNOWN, "Freeze authority: couldn't check"))
    return SafetyResult(status=status, checks=checks, sources=["rugcheck"], checked_at=now or time.time())


def make_row(cfg, snap=None, safety=None, feeds=None):
    snap = snap or hot_snap()
    mom = momentum_score(snap, [], cfg.momentum)
    safety = safety if safety is not None else passed()
    row = ScanRow(snap, mom, FilterResult(True, []), feeds or {"gt_trending"}, safety)
    row.composite = composite_score(snap, mom, safety, cfg)
    return row


def http_recorder(calls, status=200, body=None):
    def handler(request):
        calls.append((str(request.url), json.loads(request.content or b"{}")))
        return httpx.Response(status, json=body if body is not None else {"ok": True, "result": {}})
    return HttpClient(max_retries=0, transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ scoring

def test_composite_ignores_social_until_available(cfg):
    row = make_row(cfg)
    comp = row.composite
    assert comp.parts["social"] is None
    expected = (0.6 * comp.parts["momentum"] + 0.2 * comp.parts["safety"]) / 0.8
    assert comp.score == pytest.approx(expected, abs=0.1)


def test_failed_safety_scores_zero(cfg):
    bad = SafetyResult(STATUS_FAIL, [SafetyCheck("honeypot", FAIL, "HONEYPOT")], [], time.time())
    assert make_row(cfg, safety=bad).composite.score == 0


def test_unverified_safety_counts_less(cfg):
    s = hot_snap()
    ok = safety_margin(s, passed(), cfg)
    unv = safety_margin(s, passed(status=STATUS_UNVERIFIED), cfg)
    assert unv == pytest.approx(ok * 0.5, abs=0.1)


def test_risk_levels(cfg):
    assert make_row(cfg).composite.risk == "LOW"
    young = hot_snap(pair_created_at=time.time() - 1800, liquidity_usd=40000)
    comp = make_row(cfg, snap=young).composite
    assert comp.risk == "HIGH" and "thin liquidity" in comp.risk_reasons


# ------------------------------------------------------------------ trade

def test_trade_suggestion_and_costs(cfg):
    t = suggest_trade(hot_snap(price_usd=1.0, liquidity_usd=100000), cfg.trade)
    assert t.take_profit == pytest.approx(1.15) and t.stop_loss == pytest.approx(0.92)
    assert t.slippage_pct == pytest.approx(0.2)            # $50 into $100k pool, round trip
    assert t.cost_pct == pytest.approx(1.2) and not t.costs_eat_target
    thin = suggest_trade(hot_snap(liquidity_usd=1000), cfg.trade)
    assert thin.costs_eat_target
    assert estimate_slippage_pct(50, None) == 100.0


# ------------------------------------------------------------------ message

def test_alert_message_contents(cfg):
    row = make_row(cfg)
    text = format_alert(row, suggest_trade(row.snap, cfg.trade), cfg)
    for needle in ("HOT", "Solana", "Mint111", "Price", "5m", "1h", "Volume", "Liquidity",
                   "Buys/sells", "Social: not tracked yet", "Mint authority revoked",
                   "Score breakdown", "Risk", "Suggested trade", "Take profit", "Stop loss",
                   "Size $50", "Est. costs", "dexscreener.com"):
        assert needle in text, needle
    assert "Hot &lt;Token&gt;" in text             # HTML-escaped for Telegram
    plain = to_plain(text)
    assert "<b>" not in plain and "Hot <Token>" in plain


# ------------------------------------------------------------------ rules

def manager(cfg, tmp_path, calls, dry_run=False, **http_kw):
    return AlertManager(cfg, Storage(tmp_path / "a.db"), http_recorder(calls, **http_kw), dry_run)


def test_alert_sent_to_telegram_and_recorded(cfg, tmp_path):
    calls = []
    m = manager(cfg, tmp_path, calls)
    ids = m.process([make_row(cfg)])
    assert len(ids) == 1
    url, body = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == CHAT and body["parse_mode"] == "HTML" and "HOT" in body["text"]
    rec = m.storage.conn.execute("SELECT * FROM alerts").fetchone()
    assert rec["delivered"] == 1 and rec["take_profit_usd"] == pytest.approx(0.0115)
    assert "safety_pass" in json.loads(rec["signals"])


def test_below_threshold_and_failed_never_alert(cfg, tmp_path):
    calls = []
    m = manager(cfg, tmp_path, calls)
    cold = make_row(cfg, snap=hot_snap(volume_m5=5000, buys_m5=50, sells_m5=60, price_change_h1=1))
    bad = make_row(cfg, safety=SafetyResult(STATUS_FAIL, [], [], time.time()))
    assert m.process([cold, bad]) == [] and calls == []
    assert m.eligible(cold, time.time()) == "score below threshold"
    assert m.eligible(bad, time.time()) == "failed safety"


def test_cooldown_and_realert_on_jump(cfg, tmp_path):
    calls = []
    m = manager(cfg, tmp_path, calls)
    row = make_row(cfg)
    now = time.time()
    m.process([row], now)
    assert m.eligible(row, now + 60) == "cooldown"
    row.composite.score += cfg.alerts.realert_score_jump
    assert m.eligible(row, now + 60) is None
    row.composite.score -= cfg.alerts.realert_score_jump
    assert m.eligible(row, now + cfg.alerts.cooldown_minutes * 60 + 1) is None


def test_unverified_toggle(cfg, tmp_path):
    row = make_row(cfg, snap=hot_snap(), safety=passed(status=STATUS_UNVERIFIED))
    row.composite.score = 95
    m = manager(cfg, tmp_path, [])
    assert m.eligible(row, time.time()) is None
    cfg.alerts.send_unverified = False
    assert m.eligible(row, time.time()) == "unverified"


def test_max_alerts_per_scan(cfg, tmp_path):
    calls = []
    rows = [make_row(cfg, snap=hot_snap(token_address=f"M{i}")) for i in range(5)]
    manager(cfg, tmp_path, calls).process(rows)
    assert len(calls) == cfg.alerts.max_alerts_per_scan


def test_dry_run_prints_and_records(cfg, tmp_path, capsys):
    calls = []
    m = manager(cfg, tmp_path, calls, dry_run=True)
    m.process([make_row(cfg)])
    out = capsys.readouterr().out
    assert calls == [] and "dry run" in out and "Suggested trade" in out
    rec = m.storage.conn.execute("SELECT dry_run, delivered FROM alerts").fetchone()
    assert tuple(rec) == (1, 0)


# ------------------------------------------------------------------ delivery errors

def test_telegram_bad_token_message(cfg):
    sender = AlertSender(cfg, http_recorder([], status=401, body={"ok": False, "description": "Unauthorized"}), False)
    assert any("TELEGRAM_BOT_TOKEN" in r for r in sender.send_test())


def test_telegram_chat_not_found_message(cfg):
    sender = AlertSender(cfg, http_recorder([], status=400,
                                            body={"ok": False, "description": "Bad Request: chat not found"}), False)
    assert any("TELEGRAM_CHAT_ID" in r for r in sender.send_test())


def test_test_alert_success_and_missing_setup(cfg):
    calls = []
    assert AlertSender(cfg, http_recorder(calls), False).send_test() == [
        "Telegram: test message sent - check your phone!"]
    cfg.secrets = {}
    res = AlertSender(cfg, http_recorder([]), False).send_test()
    assert any("missing from .env" in r for r in res)


def test_discord_204_counts_as_sent(cfg):
    cfg.alerts.discord = True
    cfg.alerts.telegram = False
    cfg.secrets = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}

    def handler(request):
        return httpx.Response(204)
    sender = AlertSender(cfg, HttpClient(max_retries=0, transport=httpx.MockTransport(handler)), False)
    assert sender.send_test() == ["Discord: test message sent."]


# ------------------------------------------------------------------ migration + full scan

def test_old_database_gets_new_alert_columns(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
              "chain TEXT NOT NULL, token_address TEXT NOT NULL, symbol TEXT, score REAL)")
    c.commit()
    c.close()
    cols = {r[1] for r in Storage(p).conn.execute("PRAGMA table_info(alerts)")}
    assert {"pair_address", "liquidity_usd", "risk", "dry_run"} <= cols


def test_full_scan_sends_alert(cfg, tmp_path):
    cfg.alerts.score_threshold = 60
    telegram = []
    base = fake_transport([])

    def handler(request):
        url = str(request.url)
        if "rugcheck" in url:
            mint = url.split("/tokens/")[1].split("/")[0]
            r = copy.deepcopy(GOOD_RUGCHECK)
            r["mint"] = mint
            return httpx.Response(200, json=r)
        if "api.telegram.org" in url:
            telegram.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if "gopluslabs" in url or "honeypot.is" in url:
            return httpx.Response(503)
        return base.handle_request(request)

    http = HttpClient(max_retries=0, transport=httpx.MockTransport(handler))
    scanner = Scanner(cfg, Storage(tmp_path / "t.db"), http)
    scanner.scan_once()
    assert telegram and "ROCKET" in telegram[0]["text"]
    n = len(telegram)
    scanner.scan_once()                       # cooldown: no repeat
    assert len(telegram) == n


def test_secrets_are_redacted_from_logs(cfg, caplog):
    from scanner.http import redact
    assert redact("https://api.telegram.org/bot123456:AA-b_c/sendMessage") == \
        "https://api.telegram.org/bot<hidden>/sendMessage"
    assert redact("https://discord.com/api/webhooks/42/tok-EN_1") == "https://discord.com/api/webhooks/<hidden>"
    caplog.set_level("INFO")
    AlertSender(cfg, http_recorder([], status=500, body={}), False).send_test()
    assert TOKEN not in caplog.text
