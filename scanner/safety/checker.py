"""Runs the right safety checks per chain, with result caching."""
from __future__ import annotations

import logging
import time

from ..config import Config
from ..http import HttpClient
from ..models import Snapshot
from ..storage import Storage
from . import (STATUS_FAIL, STATUS_PASS, UNKNOWN, SafetyCheck, SafetyResult, combine)
from .evm import EvmSafetyChecker
from .solana import SolanaSafetyChecker

log = logging.getLogger(__name__)


class SafetyChecker:
    def __init__(self, cfg: Config, storage: Storage, http: HttpClient):
        self.cfg = cfg
        self.storage = storage
        s = cfg.safety
        self.solana = SolanaSafetyChecker(s.solana, s.max_top10_holders_pct, http)
        self.evm = EvmSafetyChecker(s.evm, s.max_top10_holders_pct, http)
        self._chains = {c.name: c for c in cfg.enabled_chains}

    def _ttl(self, status: str) -> float:
        s = self.cfg.safety
        minutes = {STATUS_PASS: s.recheck_minutes_pass,
                   STATUS_FAIL: s.recheck_minutes_fail}.get(status, s.recheck_minutes_unverified)
        return minutes * 60

    def cached(self, chain: str, address: str, now: float | None = None) -> SafetyResult | None:
        """Latest stored result, if it's still fresh."""
        now = now or time.time()
        res = self.storage.latest_safety(chain, address)
        if res and now - res.checked_at < self._ttl(res.status):
            return res
        return None

    def latest(self, chain: str, address: str) -> SafetyResult | None:
        """Latest stored result, even if stale (for display)."""
        return self.storage.latest_safety(chain, address)

    def check(self, snap: Snapshot, now: float | None = None) -> SafetyResult:
        now = now or time.time()
        chain = self._chains.get(snap.chain)
        try:
            if chain is None:
                checks, sources = [SafetyCheck("config", UNKNOWN, "Chain not configured")], []
            elif chain.type == "solana":
                checks, sources = self.solana.check(snap)
            else:
                checks, sources = self.evm.check(snap, chain.chain_id)
        except Exception:  # a bad response must never crash the scan
            log.exception("Safety check crashed for %s %s", snap.chain, snap.token_address)
            checks, sources = [SafetyCheck("error", UNKNOWN, "Safety check hit an unexpected error")], []
        result = combine(checks, sources, self.cfg.safety.strict_mode, now)
        self.storage.save_safety(snap.chain, snap.token_address, result)
        log.info("Safety %s %s (%s): %s", snap.chain, snap.symbol, snap.token_address, result.status)
        return result
