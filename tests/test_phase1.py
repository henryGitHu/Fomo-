"""Phase 1 tests: config, parsing, scoring, storage, and a full scan against
recorded-shape fixtures served through a fake HTTP transport."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from scanner.config import ConfigError, load_config
from scanner.http import HttpClient
from scanner.main import Scanner
from scanner.models import Snapshot
from scanner.scoring import basic_filters, momentum_score
from scanner.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml", env_path=ROOT / "does-not-exist.env")


def fake_transport(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        path = request.url.path
        if path == "/token-boosts/latest/v1":
            return httpx.Response(200, json=fixture("ds_boosts_latest.json"))
        if path == "/token-boosts/top/v1":
            return httpx.Response(200, json=fixture("ds_boosts_top.json"))
        if path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=fixture("ds_profiles.json"))
        if path.startswith("/tokens/v1/"):
            chain = path.split("/")[3]
            f = FIX / f"ds_tokens_{chain}.json"
            return httpx.Response(200, json=json.loads(f.read_text()) if f.exists() else [])
        if path == "/api/v2/networks/solana/trending_pools":
            return httpx.Response(200, json=fixture("gt_solana_trending.json"))
        if path == "/api/v2/networks/arbitrum/new_pools":
            return httpx.Response(429, headers={"Retry-After": "0"})
        if path.startswith("/api/v2/networks/"):
            return httpx.Response(200, json=fixture("gt_empty.json"))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def fast(cfg):
    cfg.dexscreener.requests_per_minute_discovery = 60
    cfg.dexscreener.requests_per_minute_pairs = 300
    cfg.geckoterminal.requests_per_minute = 30
    return cfg


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


# ---------------------------------------------------------------- config

def test_config_loads_defaults(cfg):
    assert [c.name for c in cfg.enabled_chains] == ["Solana", "Ethereum", "Base", "BNB Chain", "Arbitrum"]
    assert cfg.filters.min_liquidity_usd == 25000
    assert cfg.filters.min_volume_1h_usd == 50000
    assert cfg.filters.min_pair_age_minutes == 10


def test_config_error_is_plain_english(tmp_path):
    text = (ROOT / "config.yaml").read_text().replace("min_liquidity_usd: 25000", "min_liquidity_usd: lots")
    p = tmp_path / "config.yaml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="filters.min_liquidity_usd"):
        load_config(p, env_path=tmp_path / "none.env")


def test_config_bad_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("scan:\n  interval_seconds: 90\n bad_indent: 1\n")
    with pytest.raises(ConfigError, match="formatting mistake"):
        load_config(p, env_path=tmp_path / "none.env")


# ---------------------------------------------------------------- scoring

def _snap(**kw):
    base = dict(ts=time.time(), chain="Solana", token_address="A", symbol="A", name="A",
                pair_address="P", dex="d", url="", source="dexscreener")
    base.update(kw)
    return Snapshot(**base)


def test_momentum_rewards_acceleration(cfg):
    hot = _snap(volume_m5=40000, volume_h1=150000, buys_m5=180, sells_m5=70, buys_h1=1500,
                sells_h1=900, price_change_m5=6, price_change_h1=18)
    flat = _snap(volume_m5=12000, volume_h1=150000, buys_m5=100, sells_m5=100, buys_h1=900,
                 sells_h1=900, price_change_m5=0.1, price_change_h1=0.5)
    h, f = momentum_score(hot, [], cfg.momentum), momentum_score(flat, [], cfg.momentum)
    assert h.score > 60 > 10 > f.score
    assert "volume_spike" in h.signals and "buy_pressure" in h.signals


def test_momentum_uses_history_baseline(cfg):
    snap = _snap(volume_m5=30000, volume_h1=300000)
    hist = [_snap(volume_m5=10000) for _ in range(5)]
    r = momentum_score(snap, hist, cfg.momentum)
    assert r.baseline_source == "history"
    assert r.volume_ratio == pytest.approx(3.0)


def test_giant_candle_penalty(cfg):
    kw = dict(volume_m5=40000, volume_h1=150000, buys_m5=180, sells_m5=70, buys_h1=1500, sells_h1=900)
    steady = momentum_score(_snap(price_change_m5=5, price_change_h1=20, **kw), [], cfg.momentum)
    spike = momentum_score(_snap(price_change_m5=19, price_change_h1=20, **kw), [], cfg.momentum)
    assert spike.giant_candle and not steady.giant_candle
    assert spike.score < steady.score


def test_basic_filters(cfg):
    now = time.time()
    ok = _snap(liquidity_usd=30000, volume_h1=60000, pair_created_at=now - 3600)
    young = _snap(liquidity_usd=30000, volume_h1=60000, pair_created_at=now - 60)
    assert basic_filters(ok, cfg.filters, now).passed
    r = basic_filters(young, cfg.filters, now)
    assert not r.passed and "pair age" in r.reasons[0]


# ---------------------------------------------------------------- full scan

def test_full_scan_against_fixtures(cfg, tmp_path):
    cfg = fast(cfg)
    calls: list[str] = []
    http = HttpClient(max_retries=1, transport=fake_transport(calls))
    storage = Storage(tmp_path / "t.db")
    rows = Scanner(cfg, storage, http).scan_once()

    by_sym = {r[0].symbol: r for r in rows}
    # Sui boost ignored (chain not configured); others discovered.
    assert set(by_sym) == {"ROCKET", "FRESH", "BASED", "WHALE", "GTONLY"}
    rocket = by_sym["ROCKET"][0]
    # Most liquid pair where ROCKET is the base token was chosen.
    assert rocket.liquidity_usd == 210000 and rocket.source == "dexscreener"
    assert by_sym["ROCKET"][3] >= {"ds_boost_latest", "gt_trending"}
    # GTONLY had no DexScreener pair -> GeckoTerminal fallback snapshot.
    assert by_sym["GTONLY"][0].source == "geckoterminal"
    # EVM address from boosts (mixed case) matched the lowercase pair address.
    assert by_sym["BASED"][0].price_usd == pytest.approx(0.0312)
    # FRESH is 25 min old but only $40k liquidity & $70k volume -> passes; age ok.
    assert by_sym["FRESH"][2].passed
    # One batched DexScreener pair call per chain with candidates.
    assert sum("/tokens/v1/" in c for c in calls) == 3
    # 429 from GeckoTerminal was retried then abandoned without crashing.
    assert sum("arbitrum/new_pools" in c for c in calls) == 2

    n = storage.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n == 5
    # Remembered tokens are re-snapshotted next scan even if feeds go quiet.
    assert len(storage.recent_tokens("Solana", time.time() - 60)) == 3


def test_scan_survives_total_outage(cfg, tmp_path):
    cfg = fast(cfg)
    http = HttpClient(max_retries=0, transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    rows = Scanner(cfg, Storage(tmp_path / "t.db"), http).scan_once()
    assert rows == []


def test_evm_history_accumulates_across_scans(cfg, tmp_path):
    cfg = fast(cfg)
    storage = Storage(tmp_path / "t.db")
    scanner = Scanner(cfg, storage, HttpClient(max_retries=0, transport=fake_transport([])))
    scanner.scan_once()
    scanner.scan_once()
    rows = storage.conn.execute(
        "SELECT token_address, COUNT(*) n FROM snapshots WHERE chain='Base' GROUP BY 1").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("0xabc0000000000000000000000000000000000001", 2)]
