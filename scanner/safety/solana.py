"""Solana safety checks.

Primary source: RugCheck's public token report
    GET https://api.rugcheck.xyz/v1/tokens/{mint}/report
which includes mint/freeze authority, markets with LP lock %, top holders,
known accounts (AMM pools, lockers) and RugCheck's own risk list.

Fallback (if RugCheck is down or rate-limited): the Solana JSON-RPC
`getAccountInfo` call on the mint gives mint and freeze authority directly.
"""
from __future__ import annotations

import logging

from ..config import SolanaSafety
from ..http import HttpClient
from ..models import Snapshot
from . import FAIL, PASS, SOLANA_BURN_ADDRESSES, UNKNOWN, SafetyCheck, to_float

log = logging.getLogger(__name__)

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
BUCKET_RUGCHECK = "rugcheck"
BUCKET_RPC = "solana_rpc"

# RugCheck risks we already judge with our own configurable thresholds,
# so they don't also trigger the generic "danger" rule.
OWN_CHECK_KEYWORDS = ("mint authority", "freeze authority", "lp unlocked", "top 10",
                      "top holder", "single holder", "low liquidity", "low amount of lp")
# Account types in RugCheck's knownAccounts that aren't real holders.
NON_HOLDER_TYPES = {"amm", "locker", "burn", "pool"}
BONDING_CURVE_MARKETS = ("pump_fun", "pumpfun", "moonshot", "bonding")


class SolanaSafetyChecker:
    def __init__(self, cfg: SolanaSafety, max_top10_pct: float, http: HttpClient):
        self.cfg = cfg
        self.max_top10_pct = max_top10_pct
        self.http = http
        http.add_bucket(BUCKET_RUGCHECK, cfg.rugcheck_requests_per_minute)
        http.add_bucket(BUCKET_RPC, cfg.rpc_requests_per_minute)

    def check(self, snap: Snapshot) -> tuple[list[SafetyCheck], list[str]]:
        report = self.http.get_json(RUGCHECK_URL.format(mint=snap.token_address),
                                    bucket=BUCKET_RUGCHECK)
        if isinstance(report, dict) and report.get("mint"):
            return self.checks_from_report(report, snap), ["rugcheck"]

        # RugCheck unavailable -> authorities straight from the chain.
        log.info("RugCheck unavailable for %s; using Solana RPC", snap.token_address)
        info = self.fetch_mint_info(snap.token_address)
        checks = self.authority_checks(info, "Solana RPC")
        checks.append(SafetyCheck("lp_locked", UNKNOWN, "LP lock: no data (RugCheck unavailable)", optional=True))
        checks.append(SafetyCheck("top10_holders", UNKNOWN, "Top holders: no data (RugCheck unavailable)", optional=True))
        return checks, (["solana_rpc"] if info is not None else [])

    # ------------------------------------------------------------ RPC fallback
    def fetch_mint_info(self, mint: str) -> dict | None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}]}
        data = self.http.post_json(self.cfg.rpc_url, body, bucket=BUCKET_RPC)
        try:
            parsed = data["result"]["value"]["data"]["parsed"]
            if parsed.get("type") != "mint":
                return None
            info = parsed["info"]
            return {"mintAuthority": info.get("mintAuthority"),
                    "freezeAuthority": info.get("freezeAuthority")}
        except (TypeError, KeyError):
            return None

    # ------------------------------------------------------------ checks
    def authority_checks(self, info: dict | None, source: str) -> list[SafetyCheck]:
        checks = []
        for key, label, required, why in (
            ("mintAuthority", "mint_authority", self.cfg.require_mint_authority_revoked,
             "creator can still create new tokens"),
            ("freezeAuthority", "freeze_authority", self.cfg.require_freeze_authority_revoked,
             "creator can freeze wallets so they can't sell"),
        ):
            nice = label.replace("_", " ").capitalize()
            if not required:
                continue
            if info is None or key not in info:
                checks.append(SafetyCheck(label, UNKNOWN, f"{nice}: couldn't check ({source} unavailable)"))
            elif info[key]:
                checks.append(SafetyCheck(label, FAIL, f"{nice} NOT revoked - {why}"))
            else:
                checks.append(SafetyCheck(label, PASS, f"{nice} revoked"))
        return checks

    def checks_from_report(self, r: dict, snap: Snapshot) -> list[SafetyCheck]:
        token = r.get("token") if isinstance(r.get("token"), dict) else {}
        info = {}
        for key in ("mintAuthority", "freezeAuthority"):
            if key in r:
                info[key] = r[key]
            elif key in token:
                info[key] = token[key]
        checks = self.authority_checks(info, "RugCheck")

        if r.get("rugged") is True:
            checks.append(SafetyCheck("rugged", FAIL, "RugCheck flags this token as RUGGED"))

        checks.append(self._lp_check(r, snap))
        checks.append(self._holder_check(r))

        if self.cfg.fail_on_rugcheck_danger:
            risks = r.get("risks")
            if isinstance(risks, list):
                danger = [str(x.get("name", "?")) for x in risks
                          if isinstance(x, dict) and str(x.get("level", "")).lower() == "danger"
                          and not any(k in str(x.get("name", "")).lower() for k in OWN_CHECK_KEYWORDS)]
                if danger:
                    checks.append(SafetyCheck("rugcheck_danger", FAIL,
                                              "RugCheck danger: " + "; ".join(danger[:4])))
                else:
                    checks.append(SafetyCheck("rugcheck_danger", PASS, "No other RugCheck danger flags"))
            else:
                checks.append(SafetyCheck("rugcheck_danger", UNKNOWN, "RugCheck risk list missing",
                                          optional=True))
        return checks

    def _lp_check(self, r: dict, snap: Snapshot) -> SafetyCheck:
        if self.cfg.min_lp_locked_pct <= 0:
            return SafetyCheck("lp_locked", PASS, "LP lock check turned off", optional=True)
        if snap.dex and any(b in snap.dex.lower() for b in BONDING_CURVE_MARKETS):
            return SafetyCheck("lp_locked", PASS,
                               "Still on a bonding curve (no LP that can be pulled)", optional=True)
        markets = [m for m in (r.get("markets") or []) if isinstance(m, dict)]
        if not markets:
            return SafetyCheck("lp_locked", UNKNOWN, "LP lock: no market data", optional=True)

        # The pool we're actually looking at, else the most liquid one.
        market = next((m for m in markets if m.get("pubkey") == snap.pair_address), None)
        if market is None:
            def liq(m):
                lp = m.get("lp") or {}
                return (to_float(lp.get("baseUSD")) or 0) + (to_float(lp.get("quoteUSD")) or 0)
            market = max(markets, key=liq)
        mtype = str(market.get("marketType", "")).lower()
        if any(b in mtype for b in BONDING_CURVE_MARKETS) and "amm" not in mtype:
            return SafetyCheck("lp_locked", PASS,
                               "Still on a bonding curve (no LP that can be pulled)", optional=True)
        pct = to_float((market.get("lp") or {}).get("lpLockedPct"))
        if pct is None:
            return SafetyCheck("lp_locked", UNKNOWN, "LP lock: not reported", optional=True)
        if pct < self.cfg.min_lp_locked_pct:
            return SafetyCheck("lp_locked", FAIL,
                               f"Only {pct:.0f}% of LP burned/locked (need {self.cfg.min_lp_locked_pct:.0f}%) "
                               f"- liquidity could be pulled", optional=True)
        return SafetyCheck("lp_locked", PASS, f"{pct:.0f}% of LP burned/locked", optional=True)

    def _holder_check(self, r: dict) -> SafetyCheck:
        holders = r.get("topHolders")
        if not isinstance(holders, list) or not holders:
            return SafetyCheck("top10_holders", UNKNOWN, "Top holders: no data", optional=True)
        known = r.get("knownAccounts") if isinstance(r.get("knownAccounts"), dict) else {}
        excluded = set(SOLANA_BURN_ADDRESSES)
        for addr, meta in known.items():
            if isinstance(meta, dict) and str(meta.get("type", "")).lower() in NON_HOLDER_TYPES:
                excluded.add(addr)
        for m in r.get("markets") or []:
            if not isinstance(m, dict):
                continue
            for k in ("pubkey", "liquidityAAccount", "liquidityBAccount", "liquidityA", "liquidityB"):
                if m.get(k):
                    excluded.add(str(m[k]))
        real = [h for h in holders if isinstance(h, dict)
                and h.get("address") not in excluded and h.get("owner") not in excluded]
        pcts = [to_float(h.get("pct")) or 0.0 for h in real[:10]]
        total = sum(pcts)
        if total > self.max_top10_pct:
            return SafetyCheck("top10_holders", FAIL,
                               f"Top 10 wallets hold {total:.0f}% (limit {self.max_top10_pct:.0f}%)",
                               optional=True)
        return SafetyCheck("top10_holders", PASS, f"Top 10 wallets hold {total:.0f}%", optional=True)
