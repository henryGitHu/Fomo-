"""GeckoTerminal public API (free, no key, ~30 calls/minute).

Endpoints used (per apiguide.geckoterminal.com):
  GET /api/v2/networks/{network}/trending_pools?include=base_token&page=N
  GET /api/v2/networks/{network}/new_pools?include=base_token&page=N

Pool attributes give price, % change, volume, buy/sell counts, reserve (liquidity),
FDV / market cap and creation time. We use them to discover tokens, and as a
fallback snapshot when DexScreener has no pair for a token.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from ..config import Chain, Config
from ..http import HttpClient
from ..models import Candidate, Snapshot, to_float, to_int

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
BUCKET = "geckoterminal"
# Pin the API version, as GeckoTerminal's guide recommends.
HEADERS = {"Accept": "application/json;version=20230302"}

FEEDS = (
    ("use_trending", "trending_pools", "gt_trending"),
    ("use_new_pools", "new_pools", "gt_new"),
)


class GeckoTerminal:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        http.add_bucket(BUCKET, cfg.geckoterminal.requests_per_minute)

    def discover(self) -> list[Candidate]:
        out: list[Candidate] = []
        for chain in self.cfg.enabled_chains:
            if not chain.geckoterminal_id:
                continue
            for setting, endpoint, feed in FEEDS:
                if not getattr(self.cfg.geckoterminal, setting):
                    continue
                for page in range(1, self.cfg.geckoterminal.pages + 1):
                    url = f"{BASE}/networks/{chain.geckoterminal_id}/{endpoint}"
                    data = self.http.get_json(url, bucket=BUCKET, headers=HEADERS,
                                              params={"include": "base_token", "page": page})
                    if data is None:
                        break
                    out.extend(parse_pools(data, chain, feed))
        return out


def parse_pools(data, chain: Chain, feed: str, now: float | None = None) -> list[Candidate]:
    if not isinstance(data, dict):
        return []
    now = now or time.time()
    tokens: dict[str, dict] = {}
    for inc in data.get("included") or []:
        if isinstance(inc, dict) and inc.get("type") == "token":
            tokens[str(inc.get("id"))] = inc.get("attributes") or {}

    out = []
    for pool in data.get("data") or []:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        token_id = str(rel.get("id") or "")
        tok = tokens.get(token_id, {})
        address = str(tok.get("address") or "")
        if not address:
            # ids look like "<network>_<address>"; network ids may contain "_".
            prefix = f"{chain.geckoterminal_id}_"
            address = token_id[len(prefix):] if token_id.startswith(prefix) else ""
        if not address:
            continue
        symbol = str(tok.get("symbol") or "")
        name = str(tok.get("name") or attrs.get("name") or "")
        snap = pool_to_snapshot(attrs, chain, address, symbol, name, now)
        out.append(Candidate(chain=chain.name, token_address=address, symbol=symbol,
                             name=name, feeds={feed}, fallback=snap))
    return out


def pool_to_snapshot(attrs: dict, chain: Chain, address: str, symbol: str,
                     name: str, now: float) -> Snapshot:
    pc = attrs.get("price_change_percentage") or {}
    vol = attrs.get("volume_usd") or {}
    tx = attrs.get("transactions") or {}
    m5 = tx.get("m5") or {}
    h1 = tx.get("h1") or {}
    pool_addr = str(attrs.get("address") or "")
    return Snapshot(
        ts=now,
        chain=chain.name,
        token_address=address,
        symbol=symbol,
        name=name,
        pair_address=pool_addr,
        dex="",
        url=f"https://www.geckoterminal.com/{chain.geckoterminal_id}/pools/{pool_addr}",
        source="geckoterminal",
        price_usd=to_float(attrs.get("base_token_price_usd")),
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
        liquidity_usd=to_float(attrs.get("reserve_in_usd")),
        fdv_usd=to_float(attrs.get("fdv_usd")),
        market_cap_usd=to_float(attrs.get("market_cap_usd")),
        pair_created_at=_parse_time(attrs.get("pool_created_at")),
    )


def _parse_time(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
