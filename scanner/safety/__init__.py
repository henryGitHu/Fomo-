"""Token safety checks (hard gates).

Each check reports one of:
  pass     - verified OK
  fail     - verified bad -> the token must never be alerted
  unknown  - couldn't be completed (source down, no data)
Overall status:
  FAIL        if any check failed
  UNVERIFIED  if nothing failed but a required check is unknown
  PASS        otherwise
"Optional" checks (holder concentration, LP lock - "where data is available")
only make a token UNVERIFIED when safety.strict_mode is true.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
STATUS_PASS, STATUS_FAIL, STATUS_UNVERIFIED = "PASS", "FAIL", "UNVERIFIED"

# Well-known burn / null addresses, excluded from holder concentration.
EVM_BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}
SOLANA_BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111"}


@dataclass
class SafetyCheck:
    name: str            # short id, e.g. "mint_authority"
    status: str          # pass / fail / unknown
    detail: str          # plain-English explanation
    optional: bool = False
    value: float | None = None   # the measured number (holder %, tax %, LP-locked %), if any


@dataclass
class SafetyResult:
    status: str                         # PASS / FAIL / UNVERIFIED
    checks: list[SafetyCheck] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def problems(self) -> list[SafetyCheck]:
        """Checks that failed, then ones that couldn't be completed."""
        return ([c for c in self.checks if c.status == FAIL]
                + [c for c in self.checks if c.status == UNKNOWN])

    def to_json(self) -> str:
        return json.dumps({"checks": [asdict(c) for c in self.checks], "sources": self.sources})

    @classmethod
    def from_json(cls, status: str, text: str, checked_at: float) -> "SafetyResult":
        data = json.loads(text or "{}")
        return cls(status=status,
                   checks=[SafetyCheck(**c) for c in data.get("checks", [])],
                   sources=data.get("sources", []), checked_at=checked_at)


def combine(checks: list[SafetyCheck], sources: list[str], strict: bool,
            now: float) -> SafetyResult:
    if any(c.status == FAIL for c in checks):
        status = STATUS_FAIL
    elif any(c.status == UNKNOWN and (strict or not c.optional) for c in checks):
        status = STATUS_UNVERIFIED
    elif not checks:
        status = STATUS_UNVERIFIED
    else:
        status = STATUS_PASS
    return SafetyResult(status=status, checks=checks, sources=sources, checked_at=now)


def to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
