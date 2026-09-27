"""Console output of top movers."""
from __future__ import annotations

import math
from datetime import datetime

from rich import box
from rich.console import Console
from rich.table import Table

from .scoring import FilterResult, MomentumResult
from .models import Snapshot

console = Console()

Row = tuple[Snapshot, MomentumResult, FilterResult, set]

# Short labels so the table fits a normal console window.
SIGNAL_LABELS = {
    "volume_spike": "V",
    "buy_pressure": "B",
    "price_uptrend": "U",
    "giant_candle": "[red]![/red]",
}
LEGEND = ("[dim]Score = momentum 0-100 | Vol x = last-5-min volume vs normal pace | "
          "Buys = % of last-5-min trades that were buys\n"
          "Sig: V = volume 2x+ normal, B = 60%+ buys, U = up on 5m and 1h, "
          "! = one giant candle (score halved)[/dim]")


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            n = v / div
            return f"${n:.1f}{suffix}" if abs(n) < 10 else f"${n:.0f}{suffix}"
    return f"${v:.0f}"


def _price(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 1:
        return f"${v:,.4f}"
    if v == 0:
        return "$0"
    # 4 significant digits, written out in full (no scientific notation)
    decimals = min(18, -math.floor(math.log10(abs(v))) + 3)
    return f"${v:.{decimals}f}"


def _pct(v: float | None) -> str:
    if v is None:
        return "-"
    color = "green" if v > 0 else "red" if v < 0 else "white"
    text = f"{v:+.1f}%" if abs(v) < 100 else f"{v:+.0f}%"
    return f"[{color}]{text}[/{color}]"


def _ratio(r: float | None) -> str:
    return "-" if r is None else f"{r * 100:.0f}%"


def _age(snap: Snapshot) -> str:
    a = snap.age_minutes()
    if a is None:
        return "-"
    if a < 120:
        return f"{a:.0f}m"
    if a < 48 * 60:
        return f"{a / 60:.0f}h"
    return f"{a / 1440:.0f}d"


def print_top_movers(rows: list[Row], top_n: int, only_passing: bool,
                     stats: dict[str, int]) -> None:
    shown = [r for r in rows if r[2].passed] if only_passing else list(rows)
    shown.sort(key=lambda r: r[1].score, reverse=True)
    shown = shown[:top_n]

    title = (f"Top movers  {datetime.now():%Y-%m-%d %H:%M:%S}   "
             f"scanned {stats.get('snapshots', 0)} tokens, "
             f"{stats.get('passing', 0)} pass basic filters")
    table = Table(title=title, title_justify="left", header_style="bold cyan",
                  box=box.SIMPLE_HEAD, pad_edge=False, expand=False)
    for col, just in (("Score", "right"), ("Token", "left"), ("Chain", "left"),
                      ("Price", "right"), ("5m", "right"), ("1h", "right"),
                      ("Vol 5m", "right"), ("Vol 1h", "right"), ("Vol x", "right"),
                      ("Buys", "right"), ("Liq", "right"), ("MCap", "right"),
                      ("Age", "right"), ("Sig", "left")):
        table.add_column(col, justify=just, no_wrap=True, min_width=4 if col == "Sig" else None)

    for snap, mom, flt, feeds in shown:
        token = (snap.symbol or snap.token_address)[:10]
        if not flt.passed:
            token = f"[dim]{token} (filtered)[/dim]"
        sig = [SIGNAL_LABELS.get(x, x) for x in mom.signals]
        vol_x = "-" if mom.volume_ratio is None else f"{mom.volume_ratio:.1f}x"
        table.add_row(
            f"[bold]{mom.score:.0f}[/bold]", token, snap.chain, _price(snap.price_usd),
            _pct(snap.price_change_m5), _pct(snap.price_change_h1),
            _money(snap.volume_m5), _money(snap.volume_h1), vol_x, _ratio(mom.buy_ratio_m5),
            _money(snap.liquidity_usd), _money(snap.market_cap_usd or snap.fdv_usd),
            _age(snap), "".join(sig),
        )
    if not shown:
        console.print(f"[yellow]{title}\nNo tokens to show this round "
                      f"(nothing passed the basic filters).[/yellow]")
        return
    console.print(table)
    console.print(LEGEND)
    top, feeds = shown[0][0], shown[0][3]
    seen = f" (seen in: {', '.join(sorted(feeds))})" if feeds else ""
    if top.url:
        console.print(f"[dim]#1 {top.symbol}: {top.url}{seen}[/dim]")
