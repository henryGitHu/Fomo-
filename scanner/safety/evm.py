"""EVM safety checks (Ethereum, Base, BNB Chain, Arbitrum, ...).

Primary source: GoPlus token security API (free, no key, 30 calls/min)
    GET https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={addr}
Values come back as strings: "1" = yes, "0" = no, taxes as fractions ("0.05" = 5%).

Second opinion: honeypot.is (free)
    GET https://api.honeypot.is/v2/IsHoneypot?address={addr}&chainID={chain_id}
which simulates a buy and a sell. If EITHER source says honeypot, it fails.
"""
from __future__ import annotations

import logging

from ..config import EvmSafety
from ..http import HttpClient
from ..models import Snapshot
from . import EVM_BURN_ADDRESSES, FAIL, PASS, UNKNOWN, SafetyCheck, to_float

log = logging.getLogger(__name__)

GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
BUCKET_GOPLUS = "goplus"
BUCKET_HONEYPOT = "honeypot_is"

# Dangerous only while someone still owns the contract.
OWNER_FLAGS = {
    "is_mintable": "owner can mint new tokens",
    "is_blacklisted": "owner can blacklist wallets",
    "transfer_pausable": "owner can pause trading",
    "slippage_modifiable": "owner can change the tax",
    "personal_slippage_modifiable": "owner can set a tax for specific wallets",
    "trading_cooldown": "trading cooldown can be enforced",
}
# Dangerous even if ownership looks renounced.
ALWAYS_FLAGS = {
    "hidden_owner": "hidden owner",
    "can_take_back_ownership": "ownership can be reclaimed",
    "selfdestruct": "contract can self-destruct",
    "owner_change_balance": "owner can change balances",
}
LP_TAG_WORDS = ("pair", "pool", "lp", "uniswap", "pancake", "sushi", "aerodrome",
                "velodrome", "camelot", "burn", "dead", "null", "black hole")


def _flag(data: dict, key: str) -> bool | None:
    v = data.get(key)
    if v in ("1", 1, True):
        return True
    if v in ("0", 0, False):
        return False
    return None


class EvmSafetyChecker:
    def __init__(self, cfg: EvmSafety, max_top10_pct: float, http: HttpClient):
        self.cfg = cfg
        self.max_top10_pct = max_top10_pct
        self.http = http
        http.add_bucket(BUCKET_GOPLUS, cfg.goplus_requests_per_minute)
        http.add_bucket(BUCKET_HONEYPOT, cfg.honeypot_is_requests_per_minute)
        # Chains honeypot.is told us it doesn't support; not asked again this run.
        self._honeypot_unsupported: set[int] = set()

    def check(self, snap: Snapshot, chain_id: int | None) -> tuple[list[SafetyCheck], list[str]]:
        if not chain_id:
            return [SafetyCheck("config", UNKNOWN,
                                f"No chain_id set for {snap.chain} in config.yaml - can't run safety checks")], []
        addr = snap.token_address.lower()
        gp = self.fetch_goplus(chain_id, addr)
        hp = self.fetch_honeypot_is(chain_id, addr) if self.cfg.use_honeypot_is else None
        sources = (["goplus"] if gp is not None else []) + (["honeypot.is"] if hp is not None else [])
        return self.evaluate(gp, hp, snap), sources

    # ------------------------------------------------------------ fetch
    def fetch_goplus(self, chain_id: int, addr: str) -> dict | None:
        data = self.http.get_json(GOPLUS_URL.format(chain_id=chain_id), bucket=BUCKET_GOPLUS,
                                  params={"contract_addresses": addr})
        if not isinstance(data, dict):
            return None
        if data.get("code") not in (1, "1", None):
            log.info("GoPlus returned code %s for %s: %s", data.get("code"), addr, data.get("message"))
            return None
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return None
        entry = result.get(addr) or next((v for k, v in result.items() if k.lower() == addr), None)
        return entry if isinstance(entry, dict) and entry else None

    def fetch_honeypot_is(self, chain_id: int, addr: str) -> dict | None:
        if chain_id in self._honeypot_unsupported:
            return None
        # 404 = honeypot.is has no pool for this token; 400 = bad/unsupported
        # chain. Both are normal answers - GoPlus still covers the token.
        status, data = self.http.get_json_status(
            HONEYPOT_URL, bucket=BUCKET_HONEYPOT,
            params={"address": addr, "chainID": chain_id}, expected=(400, 404))
        if status == 400 and "chain" in str((data or {}).get("error", "")).lower():
            log.info("honeypot.is doesn't support chain %s; using GoPlus only for it", chain_id)
            self._honeypot_unsupported.add(chain_id)
            return None
        return data if status == 200 and isinstance(data, dict) and (
            "honeypotResult" in data or "simulationResult" in data) else None

    # ------------------------------------------------------------ evaluate
    def evaluate(self, gp: dict | None, hp: dict | None, snap: Snapshot) -> list[SafetyCheck]:
        checks = [self._honeypot(gp, hp), self._taxes(gp, hp)]
        if self.cfg.fail_if_not_open_source:
            checks.append(self._open_source(gp))
        if self.cfg.fail_on_owner_privileges:
            checks.append(self._owner(gp))
        checks.append(self._holders(gp, snap))
        return checks

    def _honeypot(self, gp, hp) -> SafetyCheck:
        verdicts: list[tuple[str, bool]] = []
        if gp:
            for key in ("is_honeypot", "cannot_sell_all"):
                f = _flag(gp, key)
                if f is not None:
                    verdicts.append(("GoPlus" if key == "is_honeypot" else "GoPlus (can't sell all)", f))
        if hp:
            hr = hp.get("honeypotResult") or {}
            if isinstance(hr.get("isHoneypot"), bool) and hp.get("simulationSuccess") is not False:
                verdicts.append(("honeypot.is", hr["isHoneypot"]))
        if not verdicts:
            return SafetyCheck("honeypot", UNKNOWN, "Honeypot: couldn't check (no data from GoPlus or honeypot.is)")
        bad = [src for src, is_bad in verdicts if is_bad]
        if bad:
            return SafetyCheck("honeypot", FAIL, "HONEYPOT - you may not be able to sell (" + ", ".join(bad) + ")")
        return SafetyCheck("honeypot", PASS,
                           "Not a honeypot (" + ", ".join(sorted({s.split(' ')[0] for s, _ in verdicts})) + ")")

    def _taxes(self, gp, hp) -> SafetyCheck:
        buys, sells = [], []
        if gp:
            b, s = to_float(gp.get("buy_tax")), to_float(gp.get("sell_tax"))
            if b is not None:
                buys.append(b * 100)
            if s is not None:
                sells.append(s * 100)
        if hp:
            sim = hp.get("simulationResult") or {}
            b, s = to_float(sim.get("buyTax")), to_float(sim.get("sellTax"))
            if b is not None:
                buys.append(b)
            if s is not None:
                sells.append(s)
        if not buys or not sells:
            return SafetyCheck("taxes", UNKNOWN, "Buy/sell tax: couldn't determine")
        buy, sell = max(buys), max(sells)
        text = f"Buy tax {buy:.1f}%, sell tax {sell:.1f}%"
        if buy > self.cfg.max_buy_tax_pct or sell > self.cfg.max_sell_tax_pct:
            return SafetyCheck("taxes", FAIL, f"{text} (limit {self.cfg.max_buy_tax_pct:.0f}%/"
                                              f"{self.cfg.max_sell_tax_pct:.0f}%)", value=max(buy, sell))
        return SafetyCheck("taxes", PASS, text, value=max(buy, sell))

    def _open_source(self, gp) -> SafetyCheck:
        f = _flag(gp or {}, "is_open_source")
        if f is None:
            return SafetyCheck("open_source", UNKNOWN, "Source code: unknown")
        if not f:
            return SafetyCheck("open_source", FAIL, "Contract source code is NOT published (can't be checked)")
        return SafetyCheck("open_source", PASS, "Contract source code published")

    def _owner(self, gp) -> SafetyCheck:
        if not gp:
            return SafetyCheck("owner_privileges", UNKNOWN, "Owner privileges: no data")
        owner = str(gp.get("owner_address") or "").lower()
        owner_live = bool(owner) and owner not in EVM_BURN_ADDRESSES
        issues = [why for key, why in ALWAYS_FLAGS.items() if _flag(gp, key)]
        if owner_live:
            issues += [why for key, why in OWNER_FLAGS.items() if _flag(gp, key)]
        known = [k for k in list(ALWAYS_FLAGS) + list(OWNER_FLAGS) if _flag(gp, k) is not None]
        if issues:
            return SafetyCheck("owner_privileges", FAIL, "Risky contract powers: " + "; ".join(issues))
        if not known:
            return SafetyCheck("owner_privileges", UNKNOWN, "Owner privileges: not reported")
        return SafetyCheck("owner_privileges", PASS,
                           "Ownership renounced" if not owner_live else "No risky owner powers found")

    def _holders(self, gp, snap: Snapshot) -> SafetyCheck:
        holders = (gp or {}).get("holders")
        if not isinstance(holders, list) or not holders:
            return SafetyCheck("top10_holders", UNKNOWN, "Top holders: no data", optional=True)
        pair = (snap.pair_address or "").lower()
        real = []
        for h in holders:
            if not isinstance(h, dict):
                continue
            a = str(h.get("address", "")).lower()
            tag = str(h.get("tag", "")).lower()
            if a in EVM_BURN_ADDRESSES or a == pair or any(w in tag for w in LP_TAG_WORDS):
                continue
            real.append(h)
        total = sum((to_float(h.get("percent")) or 0.0) for h in real[:10]) * 100
        if total > self.max_top10_pct:
            return SafetyCheck("top10_holders", FAIL,
                               f"Top 10 wallets hold {total:.0f}% (limit {self.max_top10_pct:.0f}%)",
                               optional=True, value=total)
        return SafetyCheck("top10_holders", PASS, f"Top 10 wallets hold {total:.0f}%", optional=True,
                           value=total)
