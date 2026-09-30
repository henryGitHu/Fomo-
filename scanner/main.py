"""Scanner entry point and scheduler loop.

    python -m scanner.main            run until closed
    python -m scanner.main --once     one scan, then exit
    python -m scanner.main --dry-run  print alerts instead of sending them
    python -m scanner.main --test-alert  send one test message and exit
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from . import display
from .alerts import AlertManager, AlertSender
from .config import Config, ConfigError, load_config
from .http import HttpClient, RedactingFilter
from .models import Candidate, ScanRow, Snapshot, token_key
from .safety.checker import SafetyChecker
from .scoring import basic_filters, composite_score, momentum_score
from .sources.dexscreener import DexScreener
from .sources.geckoterminal import GeckoTerminal
from .storage import Storage

log = logging.getLogger("scanner")

PRUNE_EVERY_SECONDS = 3600


def setup_logging(cfg: Config) -> None:
    cfg.logging.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        cfg.logging.log_file,
        maxBytes=int(cfg.logging.max_file_mb * 1024 * 1024),
        backupCount=cfg.logging.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(cfg.logging.level)
    # Warnings and errors also go to the console so you notice them.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)
    for h in root.handlers:
        h.addFilter(RedactingFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Scanner:
    def __init__(self, cfg: Config, storage: Storage, http: HttpClient, dry_run: bool = False):
        self.cfg = cfg
        self.storage = storage
        self.http = http
        self.dexscreener = DexScreener(cfg, http) if cfg.dexscreener.enabled else None
        self.geckoterminal = GeckoTerminal(cfg, http) if cfg.geckoterminal.enabled else None
        self.safety = SafetyChecker(cfg, storage, http) if cfg.safety.enabled else None
        self.alerts = AlertManager(cfg, storage, http, dry_run, self.safety)
        self._chain_types = {c.name: c.type for c in cfg.enabled_chains}
        self._last_prune = 0.0

    # -------------------------------------------------------------- discovery
    def discover(self) -> dict[tuple[str, str], Candidate]:
        found: list[Candidate] = []
        for source in (self.dexscreener, self.geckoterminal):
            if source is None:
                continue
            try:
                found.extend(source.discover())
            except Exception:  # never let one source kill the scan
                log.exception("Discovery failed for %s", type(source).__name__)

        merged: dict[tuple[str, str], Candidate] = {}
        for c in found:
            # Normalise EVM addresses to lowercase so history lines up across feeds.
            c.token_address = token_key(self._chain_types[c.chain], c.token_address)
            if c.fallback is not None:
                c.fallback.token_address = c.token_address
            key = (c.chain, c.token_address)
            cur = merged.get(key)
            if cur is None:
                merged[key] = c
                continue
            cur.feeds |= c.feeds
            cur.symbol = cur.symbol or c.symbol
            cur.name = cur.name or c.name
            if cur.fallback is None and c.fallback is not None:
                cur.fallback = c.fallback
        return merged

    # -------------------------------------------------------------- one scan
    def scan_once(self) -> list[ScanRow]:
        now = time.time()
        candidates = self.discover()
        for c in candidates.values():
            self.storage.upsert_token(c.chain, c.token_address, c.symbol, c.name, c.feeds, now)
        self.storage.commit()

        rows: list[ScanRow] = []
        memory_since = now - self.cfg.scan.candidate_memory_minutes * 60
        cap = self.cfg.scan.max_candidates_per_chain
        for chain in self.cfg.enabled_chains:
            current = [c for c in candidates.values() if c.chain == chain.name]
            # Tokens in more feeds first; they're the strongest discovery signal.
            current.sort(key=lambda c: len(c.feeds), reverse=True)
            chosen: dict[str, Candidate] = {}
            for c in current:
                chosen.setdefault(token_key(chain.type, c.token_address), c)
            for row in self.storage.recent_tokens(chain.name, memory_since):
                if len(chosen) >= cap:
                    break
                chosen.setdefault(token_key(chain.type, row["token_address"]),
                                  Candidate(chain=chain.name, token_address=row["token_address"],
                                            symbol=row["symbol"] or "", name=row["name"] or ""))
            picked = list(chosen.values())[:cap]
            if not picked:
                continue

            snaps: dict[str, Snapshot] = {}
            if self.dexscreener and chain.dexscreener_id:
                try:
                    snaps = self.dexscreener.snapshots(chain.name, [c.token_address for c in picked])
                except Exception:
                    log.exception("DexScreener pair lookup failed for %s", chain.name)
            for c in picked:
                snap = snaps.get(c.token_address) or c.fallback
                if snap is None:
                    continue
                snap.ts = now
                snap.symbol = snap.symbol or c.symbol
                snap.name = snap.name or c.name
                history = self.storage.snapshot_history(
                    chain.name, c.token_address,
                    since=now - self.cfg.momentum.baseline_minutes * 60, before=now)
                mom = momentum_score(snap, history, self.cfg.momentum)
                flt = basic_filters(snap, self.cfg.filters, now)
                rows.append(ScanRow(snap, mom, flt, set(c.feeds)))

        self.storage.add_snapshots([r.snap for r in rows])
        if now - self._last_prune > PRUNE_EVERY_SECONDS:
            removed = self.storage.prune_snapshots(now - self.cfg.storage.keep_snapshots_days * 86400)
            if removed:
                log.info("Pruned %d old snapshots", removed)
            self._last_prune = now
        self.storage.commit()
        self.run_safety(rows, now)
        for r in rows:
            r.composite = composite_score(r.snap, r.momentum, r.safety, self.cfg, social=None, now=now)
        try:
            self.alerts.process(rows, now)
        except Exception:  # an alert problem must never stop the scanner
            log.exception("Alert processing failed")
        log.info("Scan done: %d candidates discovered, %d snapshots, %d pass filters",
                 len(candidates), len(rows), sum(1 for r in rows if r.filters.passed))
        return rows

    # -------------------------------------------------------------- safety
    def run_safety(self, rows: list[ScanRow], now: float) -> None:
        """Attach safety results: fresh cached ones for free, then new checks
        for the highest-momentum tokens that passed the basic filters."""
        if self.safety is None:
            return
        todo: list[ScanRow] = []
        for r in rows:
            if not r.filters.passed:
                continue
            cached = self.safety.cached(r.snap.chain, r.snap.token_address, now)
            if cached is not None:
                r.safety = cached
            else:
                r.safety = self.safety.latest(r.snap.chain, r.snap.token_address)  # stale, for display
                todo.append(r)
        todo.sort(key=lambda r: r.momentum.score, reverse=True)
        for r in todo[: self.cfg.safety.max_checks_per_scan]:
            r.safety = self.safety.check(r.snap, now)


def run(cfg: Config, once: bool, dry_run: bool) -> None:
    storage = Storage(cfg.storage.database_path)
    http = HttpClient(timeout=cfg.network.timeout_seconds, max_retries=cfg.network.max_retries)
    scanner = Scanner(cfg, storage, http, dry_run=dry_run)
    chains = ", ".join(c.name for c in cfg.enabled_chains)
    display.console.print(f"[bold]Crypto Momentum Scanner[/bold] - watching: {chains}"
                          + ("  [yellow](dry run)[/yellow]" if dry_run else ""))
    display.console.print("[dim]This tool never trades. Press Ctrl+C to stop.[/dim]")
    log.info("Starting scanner (chains: %s, dry_run=%s)", chains, dry_run)
    if not dry_run:
        for problem in scanner.alerts.sender.setup_problems():
            display.console.print(f"[yellow]Alert setup: {problem}. Alerts will be shown "
                                  f"in this window instead.[/yellow]")
    try:
        while True:
            started = time.time()
            try:
                rows = scanner.scan_once()
                display.print_top_movers(
                    rows, cfg.display.top_n, cfg.display.only_passing_filters,
                    cfg.safety.enabled and cfg.safety.hide_failed)
            except Exception:
                log.exception("Scan failed; will try again next round")
            if once:
                break
            wait = max(5.0, cfg.scan.interval_seconds - (time.time() - started))
            display.console.print(f"[dim]Next scan in {wait:.0f}s...[/dim]")
            time.sleep(wait)
    except KeyboardInterrupt:
        display.console.print("\nStopped.")
    finally:
        log.info("Scanner stopped")
        http.close()
        storage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crypto momentum scanner (alerts only, never trades)")
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts to the console instead of sending them")
    parser.add_argument("--test-alert", action="store_true",
                        help="send one test message to Telegram/Discord and exit")
    parser.add_argument("--config", help="path to config.yaml (default: the one next to run.bat)")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"SETTINGS PROBLEM: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)
    if args.test_alert:
        return send_test_alert(cfg)
    run(cfg, once=args.once, dry_run=args.dry_run)
    return 0


def send_test_alert(cfg: Config) -> int:
    http = HttpClient(timeout=cfg.network.timeout_seconds, max_retries=1)
    try:
        results = AlertSender(cfg, http, dry_run=False).send_test()
    finally:
        http.close()
    for line in results or ["Nothing to test - no alert channel is set up."]:
        color = "green" if "sent" in line else "red"
        display.console.print(f"[{color}]{line}[/{color}]")
    return 0 if any("sent" in line for line in results) else 1


if __name__ == "__main__":
    sys.exit(main())
