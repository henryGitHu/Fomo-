"""Phase 4 tests: paper-trade evaluation, tracker loop and report."""
from __future__ import annotations

import json
import time

import httpx
import pytest
from rich.console import Console

from scanner import report
from scanner.config import load_config
from scanner.http import HttpClient
from scanner.models import Snapshot
from scanner.sources.dexscreener import DexScreener
from scanner.storage import Storage
from scanner.tracker import HIT_EXPIRED, HIT_NO_DATA, HIT_OPEN, HIT_SL, HIT_TP, Tracker, evaluate

from test_phase1 import ROOT, fast

T0 = 1_700_000_000.0


@pytest.fixture
def cfg():
    return fast(load_config(ROOT / "config.yaml", env_path=ROOT / "none.env"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


def alert(entry=1.0, tp=1.15, sl=0.92, cost=1.5, ts=T0):
    return {"ts": ts, "price_usd": entry, "take_profit_usd": tp, "stop_loss_usd": sl,
            "est_cost_pct": cost}


def obs(*pairs):
    """(minutes after alert, price) -> (ts, price)."""
    return [(T0 + m * 60, p) for m, p in pairs]


# ------------------------------------------------------------------ evaluate

def test_take_profit_hit_first():
    out = evaluate(alert(), obs((2, 1.05), (6, 1.20), (9, 0.80)), T0 + 600, 1.0)
    assert out["hit"] == HIT_TP
    assert out["gross_return_pct"] == pytest.approx(15.0)       # filled at TP, not 20%
    assert out["net_return_pct"] == pytest.approx(13.5)         # minus 1.5% costs


def test_stop_loss_uses_seen_price_when_gapped():
    out = evaluate(alert(), obs((3, 0.97), (5, 0.70)), T0 + 600, 1.0)
    assert out["hit"] == HIT_SL
    assert out["gross_return_pct"] == pytest.approx(-30.0)      # gapped through the -8% stop
    assert out["net_return_pct"] == pytest.approx(-31.5)


def test_checkpoints_recorded():
    readings = obs((5.5, 1.01), (15.2, 1.03), (61, 1.05), (241, 1.02))
    out = evaluate(alert(), readings, T0 + 5 * 3600, 1.0)
    assert (out["price_5m"], out["price_15m"], out["price_60m"], out["price_4h"]) == (1.01, 1.03, 1.05, 1.02)


def test_checkpoint_skipped_when_reading_too_late():
    out = evaluate(alert(), obs((20, 1.01)), T0 + 3600, 1.0)
    assert out["price_5m"] is None          # first reading 15 min late -> not a 5m price
    assert out["price_15m"] == 1.01


def test_still_open_then_expires_at_4h_price():
    readings = obs((10, 1.02), (120, 1.04), (239, 1.06))
    assert evaluate(alert(), readings, T0 + 3600, 1.0)["hit"] == HIT_OPEN
    # Just past 4h with no 4h reading yet: wait for it rather than guess.
    assert evaluate(alert(), readings, T0 + 4 * 3600 + 60, 1.0)["hit"] == HIT_OPEN
    out = evaluate(alert(), readings + obs((241.5, 1.07)), T0 + 4 * 3600 + 120, 1.0)
    assert out["hit"] == HIT_EXPIRED and out["price_4h"] == 1.07
    assert out["gross_return_pct"] == pytest.approx(7.0)


def test_expires_at_last_price_if_no_4h_reading():
    readings = obs((10, 1.02), (239, 1.06))
    out = evaluate(alert(), readings, T0 + 4 * 3600 + 31 * 60, 1.0)
    assert out["hit"] == HIT_EXPIRED and out["gross_return_pct"] == pytest.approx(6.0)


def test_no_data_after_grace():
    assert evaluate(alert(), [], T0 + 4 * 3600 + 10, 1.0)["hit"] == HIT_OPEN
    assert evaluate(alert(), [], T0 + 5 * 3600, 1.0)["hit"] == HIT_NO_DATA


def test_default_cost_when_alert_has_none():
    out = evaluate(alert(cost=None), obs((1, 1.2)), T0 + 120, 1.0)
    assert out["net_return_pct"] == pytest.approx(14.0)


# ------------------------------------------------------------------ tracker loop

def add_alert(storage, ts, chain="Solana", addr="Mint111", price=1.0, score=75, signals=None, **kw):
    values = dict(ts=ts, chain=chain, token_address=addr, symbol=addr[:4], score=score,
                  signals=json.dumps(signals or ["volume_spike", "safety_pass"]),
                  safety_status="PASS", price_usd=price, take_profit_usd=price * 1.15,
                  stop_loss_usd=price * 0.92, position_usd=50, est_cost_pct=1.2, risk="MEDIUM",
                  dry_run=0)
    values.update(kw)
    return storage.add_alert(**values)


def test_tracker_fetches_prices_and_saves_outcome(cfg, tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[{
            "chainId": "solana", "pairAddress": "P", "baseToken": {"address": "Mint111", "symbol": "MINT"},
            "priceUsd": "1.20", "liquidity": {"usd": 100000}}])

    http = HttpClient(max_retries=0, transport=httpx.MockTransport(handler))
    storage = Storage(tmp_path / "t.db")
    now = time.time()
    aid = add_alert(storage, now - 120)
    tracker = Tracker(cfg, storage, DexScreener(cfg, http))
    assert tracker.update(now) == 1
    assert any("/tokens/v1/solana/Mint111" in c for c in calls)
    row = storage.conn.execute("SELECT * FROM outcomes WHERE alert_id = ?", (aid,)).fetchone()
    assert row["hit"] == HIT_TP and row["net_return_pct"] == pytest.approx(13.8)


def test_tracker_uses_scan_snapshots_and_skips_fetch(cfg, tmp_path):
    storage = Storage(tmp_path / "t.db")
    now = time.time()
    aid = add_alert(storage, now - 600)
    storage.add_snapshots([Snapshot(ts=now - 30, chain="Solana", token_address="Mint111", symbol="M",
                                    name="M", pair_address="P", dex="d", url="", source="dexscreener",
                                    price_usd=0.9)])

    def handler(request):
        raise AssertionError("should not fetch: the scan just captured this token")

    tracker = Tracker(cfg, storage, DexScreener(cfg, HttpClient(transport=httpx.MockTransport(handler))))
    tracker.update(now)
    row = storage.conn.execute("SELECT hit, gross_return_pct FROM outcomes WHERE alert_id = ?", (aid,)).fetchone()
    assert row["hit"] == HIT_SL and row["gross_return_pct"] == pytest.approx(-10.0)


# ------------------------------------------------------------------ report

def seeded_storage(tmp_path):
    storage = Storage(tmp_path / "r.db")
    outcomes = [("Solana", 82, HIT_TP, 13.8, ["volume_spike", "buy_pressure", "safety_pass"]),
                ("Solana", 74, HIT_SL, -9.2, ["volume_spike", "safety_unverified"]),
                ("Base", 71, HIT_EXPIRED, 2.0, ["buy_pressure", "safety_pass"]),
                ("Base", 93, HIT_TP, 13.8, ["price_uptrend", "safety_pass"])]
    for i, (chain, score, hit, net, sigs) in enumerate(outcomes):
        aid = add_alert(storage, T0 + i * 60, chain=chain, addr=f"Tok{i}", score=score, signals=sigs)
        storage.save_outcome(aid, price_5m=1.02, price_15m=None, price_60m=1.05, price_4h=None,
                             hit=hit, hit_ts=T0, gross_return_pct=net + 1.2, net_return_pct=net,
                             updated_ts=T0)
    add_alert(storage, time.time(), addr="OpenOne")        # no outcome yet
    storage.commit()
    return storage


def test_report_numbers(tmp_path):
    rows = seeded_storage(tmp_path).alerts_with_outcomes()
    out = Console(record=True, width=120)
    report.build_report(rows, out)
    text = out.export_text()
    assert "Alerts" in text and "5" in text
    assert "Finished trades" in text
    assert "75%  (3 of 4)" in text                          # 3 of 4 finished trades net-positive
    assert "+5.1%" in text                                  # avg net (13.8 - 9.2 + 2 + 13.8) / 4
    assert "+$10.20" in text                                # 50 * (20.4%) total
    for section in ("By chain", "By score band", "By risk level", "By signal", "Latest alerts",
                    "Volume spike", "Safety UNVERIFIED", "90+", "70-79"):
        assert section in text, section
    assert "too few to trust" in text


def test_report_empty(tmp_path):
    out = Console(record=True, width=100)
    report.build_report([], out)
    assert "No alerts yet" in out.export_text()


def test_report_cli_runs(cfg, tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    seeded_storage(tmp_path).close()
    (tmp_path / "r.db").rename(db)
    text = (ROOT / "config.yaml").read_text().replace("data/scanner.db", str(db).replace("\\", "/"))
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(text)
    assert report.main(["--config", str(cfgfile), "--live-only"]) == 0
