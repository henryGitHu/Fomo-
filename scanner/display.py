"""Console output of top movers."""
from __future__ import annotations

import math
from datetime import datetime

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import ScanRow, Snapshot
from .safety import STATUS_FAIL, STATUS_PASS, STATUS_UNVERIFIED

console = Console()

SAFETY_LABELS = {STATUS_PASS: "[green]OK[/green]", STATUS_FAIL: "[red]FAIL[/red]",
                 STATUS_UNVERIFIED: "[yellow]UNVER[/yellow]"}

# Short labels so the table fits a normal console window.
SIGNAL_LABELS = {
    "volume_spike": "V",
    "buy_pressure": "B",
    "price_uptrend": "U",
    "giant_candle": "[red]![/red]",
}
LEGEND = ("[dim]Score = momentum 0-100.  Vol x = 5-min volume vs normal.\n"
          "Buys = % of 5-min trades that were buys.\n"
          "Sig: V = volume 2x+ normal, B = 60%+ buys,\n"
          "     U = up on 5m and 1h, ! = one giant candle (score halved)\n"
          "Safety: OK = passed all checks, UNVER = some checks couldn't be done,\n"
          "        FAIL = failed a check, - = not checked yet[/dim]")

SHORT_CHAIN = {"solana": "SOL", "ethereum": "ETH", "base": "BASE", "bnb chain": "BSC",
               "bsc": "BSC", "arbitrum": "ARB", "polygon": "POL", "avalanche": "AVAX"}


def _short_chain(name: str) -> str:
    return SHORT_CHAIN.get(name.lower(), name[:6])


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


def print_top_movers(rows: list[ScanRow], top_n: int, only_passing: bool,
                     hide_failed: bool) -> None:
    passing = sum(1 for r in rows if r.filters.passed)
    failed_safety = sum(1 for r in rows if r.safety and r.safety.status == STATUS_FAIL)
    shown = [r for r in rows if r.filters.passed] if only_passing else list(rows)
    if hide_failed:
        shown = [r for r in shown if not (r.safety and r.safety.status == STATUS_FAIL)]
    shown.sort(key=lambda r: r.momentum.score, reverse=True)
    shown = shown[:top_n]

    title = (f"Top movers  {datetime.now():%H:%M:%S}  "
             f"scanned {len(rows)}, {passing} pass filters"
             + (f", {failed_safety} failed safety" + (" (hidden)" if hide_failed else "")
                if failed_safety else ""))

    # (header, justify, drop priority: higher numbers are hidden first when narrow)
    columns = [("Score", "right", 0), ("Token", "left", 0), ("Chain", "left", 1),
               ("Price", "right", 7), ("5m", "right", 2), ("1h", "right", 2),
               ("Vol 5m", "right", 9), ("Vol 1h", "right", 6), ("Vol x", "right", 3),
               ("Buys", "right", 3), ("Liq", "right", 4), ("MCap", "right", 10),
               ("Age", "right", 8), ("Sig", "left", 1), ("Safety", "left", 0)]
    cells: list[list[str]] = []
    for row in shown:
        snap, mom, flt = row.snap, row.momentum, row.filters
        token = (snap.symbol or snap.token_address)[:10]
        if not flt.passed:
            token = f"[dim]{token}*[/dim]"
        sig = [SIGNAL_LABELS.get(x, x) for x in mom.signals]
        vol_x = "-" if mom.volume_ratio is None else f"{mom.volume_ratio:.1f}x"
        cells.append([
            f"[bold]{mom.score:.0f}[/bold]", token, _short_chain(snap.chain), _price(snap.price_usd),
            _pct(snap.price_change_m5), _pct(snap.price_change_h1),
            _money(snap.volume_m5), _money(snap.volume_h1), vol_x, _ratio(mom.buy_ratio_m5),
            _money(snap.liquidity_usd), _money(snap.market_cap_usd or snap.fdv_usd),
            _age(snap), "".join(sig),
            SAFETY_LABELS.get(row.safety.status, "?") if row.safety else "[dim]-[/dim]",
        ])

    # Hide the least important columns until the table fits the window.
    widths = [max([len(h), 4 if h == "Sig" else 0] + [Text.from_markup(r[i]).cell_len for r in cells])
              for i, (h, _, _) in enumerate(columns)]
    keep = list(range(len(columns)))
    gap = 3  # padding + separator between columns
    while (sum(widths[i] for i in keep) + gap * (len(keep) - 1) + 2 > console.width
           and any(columns[i][2] > 0 for i in keep)):
        worst = max(keep, key=lambda i: columns[i][2])
        keep.remove(worst)

    table = Table(title=title, title_justify="left", header_style="bold cyan",
                  box=box.SIMPLE_HEAD, pad_edge=False, expand=False)
    for i in keep:
        header, just, _ = columns[i]
        table.add_column(header, justify=just, no_wrap=True)
    for r in cells:
        table.add_row(*(r[i] for i in keep))
    if not shown:
        console.print(f"[yellow]{title}\nNo tokens to show this round "
                      f"(nothing passed the basic filters).[/yellow]")
        return
    console.print(table)
    console.print(LEGEND)
    if len(keep) < len(columns):
        hidden = ", ".join(columns[i][0] for i in range(len(columns)) if i not in keep)
        console.print(f"[dim]Hidden to fit the window: {hidden}. "
                      f"Make the window wider to see them.[/dim]")
    _print_safety_notes(shown)
    top = shown[0].snap
    if top.url:
        console.print(f"[dim]#1 {top.symbol}: {top.url}[/dim]")


def _print_safety_notes(shown: list[ScanRow], limit: int = 8) -> None:
    """One line per listed token whose safety isn't a clean pass."""
    lines = []
    for row in shown:
        res = row.safety
        if res is None or res.status == STATUS_PASS:
            continue
        issues = [c.detail for c in res.problems] or ["no checks could be completed"]
        color = "red" if res.status == STATUS_FAIL else "yellow"
        name = (row.snap.symbol or row.snap.token_address)[:10]
        lines.append(f"[{color}]{name} {res.status}[/{color}]: " + "; ".join(issues))
    if lines:
        console.print("[bold]Safety notes[/bold]")
        for line in lines[:limit]:
            console.print("  " + line, overflow="ellipsis", no_wrap=True)
