"""Phase 2 tests: Solana + EVM safety checks, caching, and scan integration."""
from __future__ import annotations

import copy
import time

import httpx
import pytest

from scanner import display
from scanner.config import load_config
from scanner.http import HttpClient
from scanner.main import Scanner
from scanner.models import Snapshot
from scanner.safety import STATUS_FAIL, STATUS_PASS, STATUS_UNVERIFIED
from scanner.safety.checker import SafetyChecker
from scanner.storage import Storage

from test_phase1 import ROOT, fake_transport, fast

MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
POOL = "PoolPubkey1111111111111111111111111111111111"
EVM = "0xabc0000000000000000000000000000000000001"
PAIR = "0xpair00000000000000000000000000000000000001"

GOOD_RUGCHECK = {
    "mint": MINT,
    "mintAuthority": None,
    "freezeAuthority": None,
    "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1_000_000_000, "decimals": 6},
    "rugged": False,
    "score": 301,
    "risks": [{"name": "Low amount of LP Providers", "level": "warn", "score": 300}],
    "markets": [{
        "pubkey": POOL, "marketType": "raydium_cpmm",
        "liquidityAAccount": "VaultA111", "liquidityBAccount": "VaultB111",
        "lp": {"lpLockedPct": 100.0, "baseUSD": 100000, "quoteUSD": 100000},
    }],
    "topHolders": [
        {"address": "VaultA111", "owner": "PoolAuth", "pct": 22.0, "insider": False},
        {"address": "H1", "owner": "W1", "pct": 4.0},
        {"address": "H2", "owner": "W2", "pct": 3.5},
        {"address": "H3", "owner": "W3", "pct": 3.0},
    ],
    "knownAccounts": {"PoolAuth": {"name": "Raydium", "type": "AMM"}},
}

GOOD_GOPLUS = {
    "code": 1, "message": "OK",
    "result": {EVM: {
        "is_honeypot": "0", "cannot_sell_all": "0", "buy_tax": "0.03", "sell_tax": "0.05",
        "is_open_source": "1", "owner_address": "0x0000000000000000000000000000000000000000",
        "is_mintable": "1",  # harmless: ownership renounced
        "is_blacklisted": "0", "hidden_owner": "0", "can_take_back_ownership": "0",
        "selfdestruct": "0", "transfer_pausable": "0",
        "holders": [
            {"address": PAIR, "tag": "UniswapV2", "is_contract": 1, "percent": "0.30"},
            {"address": "0x000000000000000000000000000000000000dead", "percent": "0.20"},
            {"address": "0x1111111111111111111111111111111111111111", "percent": "0.05"},
            {"address": "0x2222222222222222222222222222222222222222", "percent": "0.04"},
        ],
    }},
}
GOOD_HONEYPOT = {"honeypotResult": {"isHoneypot": False}, "simulationSuccess": True,
                 "simulationResult": {"buyTax": 3.0, "sellTax": 5.2, "transferTax": 0}}


@pytest.fixture
def cfg():
    return fast(load_config(ROOT / "config.yaml", env_path=ROOT / "none.env"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)


def make_http(routes: dict, calls: list | None = None):
    """routes: substring of URL -> JSON body (or int status code)."""
    def handler(request: httpx.Request):
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        for key, body in routes.items():
            if key in url:
                if isinstance(body, int):
                    return httpx.Response(body)
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return HttpClient(max_retries=0, transport=httpx.MockTransport(handler))


def snap(chain="Solana", addr=MINT, pair=POOL, dex="raydium"):
    return Snapshot(ts=time.time(), chain=chain, token_address=addr, symbol="TKN", name="Token",
                    pair_address=pair, dex=dex, url="", source="dexscreener",
                    liquidity_usd=100000, volume_h1=100000, pair_created_at=time.time() - 7200)


def checker(cfg, tmp_path, routes, calls=None):
    return SafetyChecker(cfg, Storage(tmp_path / "s.db"), make_http(routes, calls))


def by_name(result):
    return {c.name: c for c in result.checks}


# ------------------------------------------------------------------ Solana

def test_solana_good_token_passes(cfg, tmp_path):
    res = checker(cfg, tmp_path, {"rugcheck": GOOD_RUGCHECK}).check(snap())
    assert res.status == STATUS_PASS, res.checks
    c = by_name(res)
    # Pool vault (22%) excluded -> 4 + 3.5 + 3 = 10.5%
    assert "11%" in c["top10_holders"].detail or "10%" in c["top10_holders"].detail
    assert c["lp_locked"].status == "pass"


def test_solana_mint_authority_fails(cfg, tmp_path):
    r = copy.deepcopy(GOOD_RUGCHECK)
    r["mintAuthority"] = "CreatorWallet111"
    res = checker(cfg, tmp_path, {"rugcheck": r}).check(snap())
    assert res.status == STATUS_FAIL
    assert "NOT revoked" in by_name(res)["mint_authority"].detail


def test_solana_unlocked_lp_and_whales_fail(cfg, tmp_path):
    r = copy.deepcopy(GOOD_RUGCHECK)
    r["markets"][0]["lp"]["lpLockedPct"] = 5
    r["topHolders"].append({"address": "Whale", "owner": "WhaleOwner", "pct": 45.0})
    res = checker(cfg, tmp_path, {"rugcheck": r}).check(snap())
    c = by_name(res)
    assert res.status == STATUS_FAIL
    assert c["lp_locked"].status == "fail" and c["top10_holders"].status == "fail"


def test_solana_rugcheck_danger_flag(cfg, tmp_path):
    r = copy.deepcopy(GOOD_RUGCHECK)
    r["risks"].append({"name": "Permanent Delegate", "level": "danger"})
    r["risks"].append({"name": "Top 10 holders high ownership", "level": "danger"})  # our own check
    res = checker(cfg, tmp_path, {"rugcheck": r}).check(snap())
    d = by_name(res)["rugcheck_danger"]
    assert d.status == "fail" and "Permanent Delegate" in d.detail and "Top 10" not in d.detail


def test_solana_bonding_curve_lp_ok(cfg, tmp_path):
    r = copy.deepcopy(GOOD_RUGCHECK)
    r["markets"] = [{"pubkey": "Curve", "marketType": "pump_fun", "lp": {}}]
    res = checker(cfg, tmp_path, {"rugcheck": r}).check(snap(dex="pumpfun", pair="Curve"))
    assert by_name(res)["lp_locked"].status == "pass"


def test_solana_rugcheck_down_uses_rpc(cfg, tmp_path):
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {"value": {"data": {"parsed": {
        "type": "mint", "info": {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}}}}}}
    res = checker(cfg, tmp_path, {"rugcheck": 503, "mainnet-beta": rpc}).check(snap())
    c = by_name(res)
    assert c["mint_authority"].status == "pass" and c["freeze_authority"].status == "pass"
    # LP/holders unknown are optional -> PASS in normal mode...
    assert res.status == STATUS_PASS and res.sources == ["solana_rpc"]
    # ...but UNVERIFIED in strict mode.
    cfg.safety.strict_mode = True
    res = checker(cfg, tmp_path, {"rugcheck": 503, "mainnet-beta": rpc}).check(snap())
    assert res.status == STATUS_UNVERIFIED


def test_solana_everything_down_is_unverified(cfg, tmp_path):
    res = checker(cfg, tmp_path, {}).check(snap())
    assert res.status == STATUS_UNVERIFIED


# ------------------------------------------------------------------ EVM

def evm_snap():
    return snap(chain="Base", addr=EVM, pair=PAIR, dex="uniswap")


def test_evm_good_token_passes(cfg, tmp_path):
    res = checker(cfg, tmp_path, {"gopluslabs": GOOD_GOPLUS, "honeypot.is": GOOD_HONEYPOT}).check(evm_snap())
    assert res.status == STATUS_PASS, res.checks
    c = by_name(res)
    assert c["top10_holders"].detail == "Top 10 wallets hold 9%"   # pair + dead excluded
    assert "sell tax 5.2%" in c["taxes"].detail                     # worst of both sources
    assert c["owner_privileges"].detail == "Ownership renounced"
    assert res.sources == ["goplus", "honeypot.is"]


def test_evm_honeypot_from_either_source_fails(cfg, tmp_path):
    hp = copy.deepcopy(GOOD_HONEYPOT)
    hp["honeypotResult"]["isHoneypot"] = True
    res = checker(cfg, tmp_path, {"gopluslabs": GOOD_GOPLUS, "honeypot.is": hp}).check(evm_snap())
    assert res.status == STATUS_FAIL and "HONEYPOT" in by_name(res)["honeypot"].detail


def test_evm_high_tax_fails(cfg, tmp_path):
    gp = copy.deepcopy(GOOD_GOPLUS)
    gp["result"][EVM]["sell_tax"] = "0.25"
    res = checker(cfg, tmp_path, {"gopluslabs": gp, "honeypot.is": GOOD_HONEYPOT}).check(evm_snap())
    assert by_name(res)["taxes"].status == "fail"


def test_evm_live_owner_with_mint_fails(cfg, tmp_path):
    gp = copy.deepcopy(GOOD_GOPLUS)
    gp["result"][EVM]["owner_address"] = "0x9999999999999999999999999999999999999999"
    gp["result"][EVM]["is_blacklisted"] = "1"
    res = checker(cfg, tmp_path, {"gopluslabs": gp, "honeypot.is": GOOD_HONEYPOT}).check(evm_snap())
    d = by_name(res)["owner_privileges"].detail
    assert res.status == STATUS_FAIL and "mint" in d and "blacklist" in d


def test_evm_goplus_down_honeypot_only_is_unverified(cfg, tmp_path):
    res = checker(cfg, tmp_path, {"gopluslabs": 500, "honeypot.is": GOOD_HONEYPOT}).check(evm_snap())
    c = by_name(res)
    assert c["honeypot"].status == "pass" and c["taxes"].status == "pass"
    assert c["open_source"].status == "unknown"
    assert res.status == STATUS_UNVERIFIED


def test_evm_uses_configured_chain_id(cfg, tmp_path):
    calls = []
    checker(cfg, tmp_path, {"gopluslabs": GOOD_GOPLUS, "honeypot.is": GOOD_HONEYPOT}, calls).check(evm_snap())
    assert any("/token_security/8453" in c for c in calls)
    assert any("chainID=8453" in c for c in calls)


# ------------------------------------------------------------------ caching + scan

def test_results_are_cached(cfg, tmp_path):
    calls = []
    sc = checker(cfg, tmp_path, {"rugcheck": GOOD_RUGCHECK}, calls)
    s = snap()
    sc.check(s)
    assert sc.cached(s.chain, s.token_address) is not None
    assert sc.cached(s.chain, s.token_address, now=time.time() + 2 * 3600) is None  # PASS ttl = 60m


def test_scan_runs_safety_on_passing_tokens(cfg, tmp_path, capsys):
    rugcheck_calls = []
    base = fake_transport([])

    def handler(request):
        url = str(request.url)
        if "rugcheck" in url:
            rugcheck_calls.append(url)
            mint = url.split("/tokens/")[1].split("/")[0]
            r = copy.deepcopy(GOOD_RUGCHECK)
            r["mint"] = mint
            if mint.startswith("NewTok"):
                r["freezeAuthority"] = "Creator"
            return httpx.Response(200, json=r)
        if "gopluslabs" in url or "honeypot.is" in url:
            return httpx.Response(503)
        return base.handle_request(request)

    http = HttpClient(max_retries=0, transport=httpx.MockTransport(handler))
    storage = Storage(tmp_path / "t.db")
    scanner = Scanner(cfg, storage, http)
    rows = scanner.scan_once()
    by_sym = {r.snap.symbol: r for r in rows}
    assert by_sym["ROCKET"].safety.status == STATUS_PASS
    assert by_sym["FRESH"].safety.status == STATUS_FAIL      # freeze authority
    assert by_sym["WHALE"].safety.status == STATUS_UNVERIFIED  # EVM sources down
    first = len(rugcheck_calls)

    # Second scan: PASS/FAIL come from the cache, no new RugCheck calls.
    rows = scanner.scan_once()
    assert len(rugcheck_calls) == first
    assert {r.snap.symbol: r.safety.status for r in rows if r.safety}["FRESH"] == STATUS_FAIL

    display.console.width = 120
    display.print_top_movers(rows, 15, True, hide_failed=True)
    out = capsys.readouterr().out
    assert "FRESH" not in out.split("Safety notes")[0]   # failed -> hidden
    assert "UNVER" in out and "Safety notes" in out


def test_max_checks_per_scan(cfg, tmp_path):
    cfg.safety.max_checks_per_scan = 1
    calls = []
    base = fake_transport([])

    def handler(request):
        url = str(request.url)
        if "rugcheck" in url or "gopluslabs" in url or "honeypot.is" in url:
            calls.append(url)
            return httpx.Response(503)
        return base.handle_request(request)

    Scanner(cfg, Storage(tmp_path / "t.db"),
            HttpClient(max_retries=0, transport=httpx.MockTransport(handler))).scan_once()
    # Exactly one token checked (rugcheck + rpc fallback, or goplus + honeypot.is).
    assert len(calls) <= 2
