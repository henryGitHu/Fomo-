"""DexScreener public API (free, no key).

Endpoints used (per DexScreener's API reference, docs.dexscreener.com/api/reference):
  GET /token-profiles/latest/v1               - newest token profiles   (60 req/min)
  GET /token-boosts/latest/v1                 - newest boosted tokens   (60 req/min)
  GET /token-boosts/top/v1                    - most-boosted tokens     (60 req/min)
  GET /tokens/v1/{chainId}/{addr1,addr2,...}  - pairs for up to 30 tokens (300 req/min)

The first three are cross-chain lists; we keep only entries whose chainId is
one of the enabled chains in config.yaml.
"""
from __future__ import annotations

import logging
import time

from ..config import Config
from ..http import HttpClient
from ..models import Candidate, Snapshot, same_address, to_float, to_int

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
BUCKET_DISCOVERY = "dexscreener_discovery"
BUCKET_PAIRS = "dexscreener_pairs"
MAX_TOKENS_PER_CALL = 30

DISCOVERY_FEEDS = (
    ("use_boosts_latest", "/token-boosts/latest/v1", "ds_boost_latest"),
    ("use_boosts_top", "/token-boosts/top/v1", "ds_boost_top"),
    ("use_token_profiles", "/token-profiles/latest/v1", "ds_profile"),
)


class DexScreener:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        http.add_bucket(BUCKET_DISCOVERY, cfg.dexscreener.requests_per_minute_discovery)
        http.add_bucket(BUCKET_PAIRS, cfg.dexscreener.requests_per_minute_pairs)

    # ------------------------------------------------------------------ discovery
    def discover(self) -> list[Candidate]:
        """Tokens from the boosted / new-profile feeds on enabled chains."""
        found: list[Candidate] = []
        for setting, path, feed in DISCOVERY_FEEDS:
            if not getattr(self.cfg.dexscreener, setting):
                continue
            data = self.http.get_json(BASE + path, bucket=BUCKET_DISCOVERY)
            found.extend(self.parse_discovery(data, feed))
        return found

    def parse_discovery(self, data, feed: str) -> list[Candidate]:
        if isinstance(data, dict):  # tolerate a single object
            data = [data]
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            chain = self.cfg.chain_by_dexscreener_id(str(item.get("chainId", "")))
            addr = str(item.get("tokenAddress") or "").strip()
            if chain and addr:
                out.append(Candidate(chain=chain.name, token_address=addr, feeds={feed}))
        return out

    # ------------------------------------------------------------------ pair data
    def snapshots(self, chain_name: str, addresses: list[str]) -> dict[str, Snapshot]:
        """Best (most liquid) pair for each token address -> Snapshot."""
        chain = next(c for c in self.cfg.enabled_chains if c.name == chain_name)
        if not chain.dexscreener_id or not addresses:
            return {}
        result: dict[str, Snapshot] = {}
        for i in range(0, len(addresses), MAX_TOKENS_PER_CALL):
            batch = addresses[i:i + MAX_TOKENS_PER_CALL]
            url = f"{BASE}/tokens/v1/{chain.dexscreener_id}/{','.join(batch)}"
            data = self.http.get_json(url, bucket=BUCKET_PAIRS)
            if data is None:
                continue
            result.update(self.parse_pairs(data, chain_name, chain.type, batch))
        return result

    def parse_pairs(self, data, chain_name: str, chain_type: str,
                    addresses: list[str], now: float | None = None) -> dict[str, Snapshot]:
        if isinstance(data, dict):
            data = data.get("pairs") or []
        if not isinstance(data, list):
            return {}
        now = now or time.time()
        best: dict[str, tuple[float, Snapshot]] = {}
        for pair in data:
            if not isinstance(pair, dict):
                continue
            base = pair.get("baseToken") or {}
            base_addr = str(base.get("address") or "")
            # Only use pairs where our token is the *base* token, so the
            # price and price change are for our token, not its partner.
            match = next((a for a in addresses if same_address(chain_type, a, base_addr)), None)
            if match is None:
                continue
            snap = pair_to_snapshot(pair, chain_name, match, now)
            liq = snap.liquidity_usd or 0.0
            if match not in best or liq > best[match][0]:
                best[match] = (liq, snap)
        return {addr: snap for addr, (_, snap) in best.items()}


def pair_to_snapshot(pair: dict, chain_name: str, token_address: str, now: float) -> Snapshot:
    base = pair.get("baseToken") or {}
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}
    liq = pair.get("liquidity") or {}
    created_ms = to_float(pair.get("pairCreatedAt"))
    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}
    return Snapshot(
        ts=now,
        chain=chain_name,
        token_address=token_address,
        symbol=str(base.get("symbol") or ""),
        name=str(base.get("name") or ""),
        pair_address=str(pair.get("pairAddress") or ""),
        dex=str(pair.get("dexId") or ""),
        url=str(pair.get("url") or ""),
        source="dexscreener",
        price_usd=to_float(pair.get("priceUsd")),
        price_change_m5=to_float(pc.get("m5")),
        price_change_h1=to_float(pc.get("h1")),
        price_change_h6=to_float(pc.get("h6")),
        volume_m5=to_float(vol.get("m5")),
        volume_h1=to_float(vol.get("h1")),
        volume_h6=to_float(vol.get("h6")),
        volume_h24=to_float(vol.get("h24")),
        buys_m5=to_int(m5.get("buys")),
        sells_m5=to_int(m5.get("sells")),
        buys_h1=to_int(h1.get("buys")),
        sells_h1=to_int(h1.get("sells")),
        liquidity_usd=to_float(liq.get("usd")),
        fdv_usd=to_float(pair.get("fdv")),
        market_cap_usd=to_float(pair.get("marketCap")),
        pair_created_at=created_ms / 1000.0 if created_ms else None,
    )
