"""Plain data containers shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """A token that showed up in at least one discovery feed this scan."""
    chain: str                 # chain name from config (e.g. "Solana")
    token_address: str
    symbol: str = ""
    name: str = ""
    feeds: set[str] = field(default_factory=set)   # e.g. {"ds_boost_top", "gt_trending"}
    # Pool data from GeckoTerminal, used only if DexScreener has no pair for it.
    fallback: "Snapshot | None" = None


@dataclass
class Snapshot:
    """Market state of one token (its most liquid pool) at one moment."""
    ts: float                  # unix seconds when we captured it
    chain: str
    token_address: str
    symbol: str
    name: str
    pair_address: str
    dex: str
    url: str
    source: str                # "dexscreener" or "geckoterminal"
    price_usd: float | None = None
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    price_change_h6: float | None = None
    volume_m5: float | None = None
    volume_h1: float | None = None
    volume_h6: float | None = None
    volume_h24: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    pair_created_at: float | None = None   # unix seconds

    def age_minutes(self, now: float | None = None) -> float | None:
        if not self.pair_created_at:
            return None
        return max(0.0, ((now or self.ts) - self.pair_created_at) / 60.0)


def to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value) -> int | None:
    f = to_float(value)
    return None if f is None else int(f)


def same_address(chain_type: str, a: str, b: str) -> bool:
    """EVM addresses are case-insensitive; Solana addresses are not."""
    if chain_type == "evm":
        return a.lower() == b.lower()
    return a == b


def token_key(chain_type: str, address: str) -> str:
    return address.lower() if chain_type == "evm" else address


@dataclass
class ScanRow:
    """Everything we know about one token after a scan."""
    snap: Snapshot
    momentum: "object"            # scoring.MomentumResult
    filters: "object"             # scoring.FilterResult
    feeds: set[str] = field(default_factory=set)
    safety: "object | None" = None  # safety.SafetyResult, None = not checked yet
    composite: "object | None" = None  # scoring.CompositeResult (phase 3)
    social: "object | None" = None     # social.SocialResult, None = no mentions / no social source
