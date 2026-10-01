"""Social signals: extracting token mentions from text and scoring "buzz".

Extraction (any source):
  * EVM contract addresses      0x + 40 hex characters
  * Solana addresses            base58, decoding to exactly 32 bytes
  * $TICKER cashtags            e.g. $PEPE (plain words are too ambiguous)
If a message names an address, its tickers are ignored (they almost always
refer to the same token), so one message never counts twice.

Scoring (per token, at scan time):
  * Matches by contract address count fully; ticker-only matches count at
    a reduced weight because tickers collide constantly.
  * Shill filtering: identical copy-pasted texts count once; each author
    (or channel) counts at most once per window, whatever they post.
  * Score = acceleration (last N minutes vs the normal rate over the prior
    hours) blended with breadth (how many different sources). Raw counts
    alone never score high.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
URL_RE = re.compile(r"https?://\S+")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

MATCH_ADDRESS, MATCH_TICKER = "address", "ticker"


def _b58_len(s: str) -> int | None:
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return len(raw) + (len(s) - len(s.lstrip("1")))


def is_solana_address(s: str) -> bool:
    # Real addresses mix digits/upper/lower; this also rules out long words.
    if not (any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)):
        return False
    return _b58_len(s) == 32


@dataclass
class ExtractedMention:
    match_type: str          # address / ticker
    value: str               # address (EVM lower-cased) or ticker (upper-cased)
    chain_hint: str | None   # "solana" / "evm" for addresses


def extract_mentions(text: str, ignore_tickers: set[str] | frozenset = frozenset()) -> list[ExtractedMention]:
    if not text:
        return []
    found: dict[tuple[str, str], ExtractedMention] = {}
    for m in EVM_RE.findall(text):
        found[(MATCH_ADDRESS, m.lower())] = ExtractedMention(MATCH_ADDRESS, m.lower(), "evm")
    for m in SOL_RE.findall(text):
        if not m.startswith("0x") and is_solana_address(m):
            found[(MATCH_ADDRESS, m)] = ExtractedMention(MATCH_ADDRESS, m, "solana")
    if not found:
        for t in TICKER_RE.findall(text):
            t = t.upper()
            if t not in ignore_tickers and not t.isdigit():
                found[(MATCH_TICKER, t)] = ExtractedMention(MATCH_TICKER, t, None)
    return list(found.values())


def text_hash(text: str) -> str:
    """Fingerprint for spotting copy-pasted shill messages (ignores links/case/spacing)."""
    norm = " ".join(URL_RE.sub("", text or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

@dataclass
class SocialResult:
    score: float                  # 0-100
    recent: float                 # weighted, de-duplicated mentions in the recent window
    normal: float                 # average for a window of the same length before that
    sources: int                  # distinct authors/channels in the recent window
    match_type: str               # "address" if any address match, else "ticker"
    channels: list[str] = field(default_factory=list)

    def describe(self, recent_minutes: int) -> str:
        kind = "" if self.match_type == MATCH_ADDRESS else " (ticker matches only - weaker)"
        return (f"{self.score:.0f}/100 - {self.recent:g} mentions in last {recent_minutes}m "
                f"vs {self.normal:.1f} normal, from {self.sources} source{'s' if self.sources != 1 else ''}{kind}")


class SocialIndex:
    """All recent mentions, indexed so each token can be scored quickly."""

    def __init__(self, mentions, now: float, settings):
        self.now = now
        self.s = settings
        self.ignore = {t.upper() for t in settings.ignore_tickers}
        self.by_address: dict[str, list] = {}
        self.by_ticker: dict[str, list] = {}
        for m in mentions:
            if m["match_type"] == MATCH_ADDRESS and m["token_address"]:
                self.by_address.setdefault(m["token_address"].lower(), []).append(m)
            elif m["match_type"] == MATCH_TICKER and m["ticker"]:
                self.by_ticker.setdefault(m["ticker"].upper(), []).append(m)

    def _weighted(self, mentions, start: float, end: float) -> tuple[float, set[str], list[str]]:
        seen_texts: set[str] = set()
        per_author: dict[str, float] = {}
        channels: set[str] = set()
        for m in sorted(mentions, key=lambda r: r["ts"]):
            if not (start < m["ts"] <= end):
                continue
            if m["text_hash"] in seen_texts:          # copy-paste spam counts once
                continue
            seen_texts.add(m["text_hash"])
            w = 1.0 if m["match_type"] == MATCH_ADDRESS else self.s.ticker_only_weight
            age = m["author_age_days"]
            if age is not None and age < self.s.new_account_days:
                w *= self.s.new_account_weight
            author = m["author"] or m["channel"] or "?"
            per_author[author] = max(per_author.get(author, 0.0), w)   # one vote per author
            channels.add(m["channel"] or "?")
        return sum(per_author.values()), set(per_author), sorted(channels)

    def score(self, token_keys: set[str], symbol: str | None) -> SocialResult | None:
        mentions = []
        for k in token_keys:
            if k:
                mentions += self.by_address.get(k.lower(), [])
        has_address = bool(mentions)
        sym = (symbol or "").upper()
        if sym and sym not in self.ignore:
            mentions += self.by_ticker.get(sym, [])
        if not mentions:
            return None
        r_len = self.s.recent_minutes * 60
        b_len = self.s.baseline_hours * 3600
        recent, authors, channels = self._weighted(mentions, self.now - r_len, self.now)
        base, _, _ = self._weighted(mentions, self.now - r_len - b_len, self.now - r_len)
        normal = base / (b_len / r_len)
        if recent <= 0:
            score = 0.0
        else:
            accel = (recent + 0.5) / (normal + 0.5)
            accel_part = min(1.0, max(0.0, math.log(accel) / math.log(self.s.full_score_acceleration)))
            breadth = min(1.0, len(authors) / self.s.full_score_sources)
            score = 100 * (0.6 * accel_part + 0.4 * breadth)
        return SocialResult(score=round(score, 1), recent=round(recent, 2), normal=round(normal, 2),
                            sources=len(authors),
                            match_type=MATCH_ADDRESS if has_address else MATCH_TICKER,
                            channels=channels)
