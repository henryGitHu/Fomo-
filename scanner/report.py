"""Paper-trading performance report.

    python -m scanner.report              all alerts
    python -m scanner.report --days 7     only the last 7 days
    python -m scanner.report --live-only  leave out alerts made in --dry-run mode

Shows how the suggested trades in your alerts would have done, after
estimated fees and slippage, so you can tune thresholds before risking money.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime

from rich import box
from rich.console import Console
from rich.table import Table

from .config import ConfigError, load_config
from .storage import Storage
from .tracker import CHECKPOINTS, FINISHED, HIT_NO_DATA, HIT_OPEN, HIT_SL, HIT_TP

console = Console()

SCORE_BANDS = (("under 50", 0, 50), ("50-59", 50, 60), ("60-69", 60, 70),
               ("70-79", 70, 80), ("80-89", 80, 90), ("90+", 90, 101))
SIGNAL_NAMES = {
    "volume_spike": "Volume spike", "buy_pressure": "Buyers dominating",
    "price_uptrend": "Steady uptrend", "giant_candle": "One giant candle",
    "ds_boost_latest": "Seen: DexScreener boosted", "ds_boost_top": "Seen: DexScreener top boosts",
    "ds_profile": "Seen: new DexScreener profile", "gt_trending": "Seen: GeckoTerminal trending",
    "gt_new": "Seen: GeckoTerminal new pool",
    "safety_pass": "Safety PASSED", "safety_unverified": "Safety UNVERIFIED",
    "social_buzz": "Telegram buzz (social 50+)",
}


def _pct(v: float | None, color: bool = True) -> str:
    if v is None:
        return "-"
    text = f"{v:+.1f}%"
    if not color:
        return text
    return f"[green]{text}[/green]" if v > 0 else f"[red]{text}[/red]" if v < 0 else text


def _usd(v: float) -> str:
    text = f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    return f"[green]{text}[/green]" if v > 0 else f"[red]{text}[/red]" if v < 0 else text


def _stats(rows) -> dict:
    nets = [r["net_return_pct"] for r in rows]
    pnl = sum((r["net_return_pct"] / 100) * (r["position_usd"] or 0) for r in rows)
    return {
        "n": len(rows),
        "wins": sum(1 for x in nets if x > 0),
        "win_rate": (sum(1 for x in nets if x > 0) / len(nets) * 100) if nets else None,
        "avg": statistics.fmean(nets) if nets else None,
        "median": statistics.median(nets) if nets else None,
        "pnl": pnl,
    }


def _group_table(title: str, groups: dict[str, list]) -> Table:
    t = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD, header_style="bold cyan")
    for col, just in (("Group", "left"), ("Trades", "right"), ("Win rate", "right"),
                      ("Avg net", "right"), ("Total $", "right")):
        t.add_column(col, justify=just)
    for name, rows in groups.items():
        if not rows:
            continue
        s = _stats(rows)
        t.add_row(name, str(s["n"]), f"{s['win_rate']:.0f}%", _pct(s["avg"]), _usd(s["pnl"]))
    return t


def build_report(rows, out: Console = console) -> None:
    finished = [r for r in rows if r["hit"] in FINISHED and r["net_return_pct"] is not None]
    open_ = [r for r in rows if r["hit"] in (None, HIT_OPEN)]
    no_data = [r for r in rows if r["hit"] == HIT_NO_DATA]

    out.rule("[bold]Paper-trading report[/bold]")
    out.print(f"[dim]Generated {datetime.now():%Y-%m-%d %H:%M}. These are SIMULATED results of the "
              f"suggested trades - nothing was bought or sold.[/dim]\n")

    if not rows:
        out.print("No alerts yet. Leave run.bat running; every alert is tracked automatically.")
        return

    s = _stats(finished)
    summary = Table(box=box.SIMPLE, show_header=False)
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("Alerts", str(len(rows)))
    summary.add_row("Finished trades", str(len(finished)))
    summary.add_row("Still open (under 4h old)", str(len(open_)))
    if no_data:
        summary.add_row("No price data (token vanished?)", str(len(no_data)))
    if finished:
        tp = sum(1 for r in finished if r["hit"] == HIT_TP)
        sl = sum(1 for r in finished if r["hit"] == HIT_SL)
        summary.add_row("Win rate (net of costs)", f"{s['win_rate']:.0f}%  ({s['wins']} of {s['n']})")
        summary.add_row("Hit take-profit / stop-loss / timed out",
                        f"{tp} / {sl} / {len(finished) - tp - sl}")
        summary.add_row("Average net return per trade", _pct(s["avg"]))
        summary.add_row("Median net return", _pct(s["median"]))
        best = max(finished, key=lambda r: r["net_return_pct"])
        worst = min(finished, key=lambda r: r["net_return_pct"])
        summary.add_row("Best", f"{_pct(best['net_return_pct'])} ({best['symbol']}, {best['chain']})")
        summary.add_row("Worst", f"{_pct(worst['net_return_pct'])} ({worst['symbol']}, {worst['chain']})")
        summary.add_row("Total if every suggestion was taken", _usd(s["pnl"]))
    out.print(summary)

    # Average move after the alert, regardless of TP/SL.
    moves = Table(title="Price move after alert (all alerts with data)", title_justify="left",
                  box=box.SIMPLE_HEAD, header_style="bold cyan")
    for col in ("", "+5 min", "+15 min", "+60 min", "+4 hours"):
        moves.add_column(col, justify="right")
    avg_row, up_row = ["Average move"], ["Share that were up"]
    for name, _ in CHECKPOINTS:
        changes = [(r[name] / r["price_usd"] - 1) * 100 for r in rows
                   if r[name] and r["price_usd"]]
        avg_row.append(_pct(statistics.fmean(changes)) if changes else "-")
        up_row.append(f"{sum(c > 0 for c in changes) / len(changes) * 100:.0f}% of {len(changes)}"
                      if changes else "-")
    moves.add_row(*avg_row)
    moves.add_row(*up_row)
    out.print(moves)

    if finished:
        chains: dict[str, list] = {}
        for r in finished:
            chains.setdefault(r["chain"], []).append(r)
        out.print(_group_table("By chain", dict(sorted(chains.items()))))

        bands = {label: [r for r in finished if lo <= (r["score"] or 0) < hi]
                 for label, lo, hi in SCORE_BANDS}
        out.print(_group_table("By score band", bands))

        risk: dict[str, list] = {}
        for r in finished:
            risk.setdefault(r["risk"] or "?", []).append(r)
        out.print(_group_table("By risk level", {k: risk.get(k, []) for k in ("LOW", "MEDIUM", "HIGH", "?")}))

        signals: dict[str, list] = {}
        for r in finished:
            try:
                sigs = json.loads(r["signals"] or "[]")
            except ValueError:
                sigs = []
            for sig in sigs:
                signals.setdefault(SIGNAL_NAMES.get(sig, sig), []).append(r)
        out.print(_group_table("By signal (a trade counts in every signal it had)",
                               dict(sorted(signals.items(), key=lambda kv: -len(kv[1])))))

    recent = Table(title="Latest alerts", title_justify="left", box=box.SIMPLE_HEAD,
                   header_style="bold cyan")
    for col, just in (("When", "left"), ("Token", "left"), ("Chain", "left"), ("Score", "right"),
                      ("Result", "left"), ("Net", "right")):
        recent.add_column(col, justify=just)
    for r in rows[-10:][::-1]:
        recent.add_row(datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
                       (r["symbol"] or "?")[:10], r["chain"], f"{r['score'] or 0:.0f}",
                       {"TP": "take-profit", "SL": "stop-loss", "EXPIRED": "timed out (4h)",
                        "NO_DATA": "no data"}.get(r["hit"] or "", "open"),
                       _pct(r["net_return_pct"]))
    out.print(recent)

    out.print("[bold]How to read this[/bold]")
    if len(finished) < 30:
        out.print(f"  [yellow]Only {len(finished)} finished trades so far - too few to trust. "
                  f"Aim for at least 30-50 before changing settings or risking money.[/yellow]")
    out.print("  - 'Net' already subtracts the estimated fees and slippage shown in each alert.\n"
              "  - A trade ends at take-profit, stop-loss, or after 4 hours at whatever the price was.\n"
              "  - Prices are sampled about every 90 seconds, so very fast spikes can be missed.\n"
              "  - If a score band or signal keeps losing, raise alerts.score_threshold or adjust\n"
              "    the weights in config.yaml, then compare again after more alerts.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-trading performance report")
    parser.add_argument("--days", type=float, help="only include alerts from the last N days")
    parser.add_argument("--live-only", action="store_true",
                        help="leave out alerts created in --dry-run mode")
    parser.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"SETTINGS PROBLEM: {exc}", file=sys.stderr)
        return 2
    if not cfg.storage.database_path.exists():
        console.print("No database yet - run run.bat first so there's something to report on.")
        return 0
    storage = Storage(cfg.storage.database_path)
    try:
        since = time.time() - args.days * 86400 if args.days else 0
        rows = storage.alerts_with_outcomes(since)
        if args.live_only:
            rows = [r for r in rows if not r["dry_run"]]
        build_report(rows)
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
