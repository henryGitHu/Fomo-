"""Paper-trade P&L chart (PNG) for the Telegram/Discord summaries.

Two stacked panels on the same time axis (never two y-scales on one plot):
  top    - running net P&L in dollars through the day (a step line)
  bottom - each finished trade's net result in dollars (win / loss bars)
Win/loss use the validated blue/red diverging pair; text stays in ink colors,
and the legend + signed values mean color is never the only cue.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

log = logging.getLogger(__name__)

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
WIN = "#2a78d6"     # diverging pair, positive pole (validated vs the light surface)
LOSS = "#e34948"    # diverging pair, negative pole
LINE = "#2a78d6"


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def render_pnl_chart(trades: list[tuple[float, float, str]], title: str, subtitle: str) -> bytes | None:
    """trades: (close unix time, net dollars, symbol), oldest first.

    Returns PNG bytes, or None if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        log.warning("matplotlib is not installed - run setup.bat again to enable P&L charts")
        return None

    times = [datetime.fromtimestamp(t) for t, _, _ in trades]
    pnl = [v for _, v, _ in trades]
    running, total = [], 0.0
    for v in pnl:
        total += v
        running.append(total)

    plt.rcParams.update({"font.size": 10, "axes.edgecolor": BASELINE, "axes.labelcolor": INK_2,
                         "xtick.color": MUTED, "ytick.color": MUTED, "font.family": "DejaVu Sans"})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6.2), dpi=150, sharex=True,
                                   gridspec_kw={"height_ratios": [3, 2], "hspace": 0.22})
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax1, ax2):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(BASELINE)
        ax.spines["bottom"].set_color(BASELINE)
        ax.tick_params(length=0)
        ax.yaxis.set_major_formatter(lambda v, _: _usd(v).replace(".00", ""))

    # Top: running total, starting from $0 just before the first close.
    if times:
        start = times[0].replace(second=0, microsecond=0)
        xs = [start] + times
        ys = [0.0] + running
        ax1.step(xs, ys, where="post", color=LINE, linewidth=2)
        ax1.plot([times[-1]], [running[-1]], "o", color=LINE, markersize=6,
                 markeredgecolor=SURFACE, markeredgewidth=2)
        ax1.annotate(_usd(running[-1]), (times[-1], running[-1]), xytext=(8, 0),
                     textcoords="offset points", va="center", color=INK, fontweight="bold")
    ax1.axhline(0, color=BASELINE, linewidth=1)
    ax1.set_ylabel("Running total")

    # Bottom: one bar per trade at its close time.
    if times:
        span = (times[-1] - times[0]).total_seconds() if len(times) > 1 else 3600
        width_days = max(span / max(len(times), 1) / 2.5, 120) / 86400
        colors = [WIN if v >= 0 else LOSS for v in pnl]
        ax2.bar(times, pnl, width=width_days, color=colors, edgecolor=SURFACE, linewidth=1)
        # Label only the best and worst trades (selective direct labels).
        for idx in {pnl.index(max(pnl)), pnl.index(min(pnl))}:
            v = pnl[idx]
            ax2.annotate(f"{trades[idx][2][:8]} {_usd(v)}", (times[idx], v),
                         xytext=(0, 4 if v >= 0 else -4), textcoords="offset points",
                         ha="center", va="bottom" if v >= 0 else "top", color=INK_2, fontsize=8)
        ax2.legend(handles=[Patch(color=WIN, label="Win"), Patch(color=LOSS, label="Loss")],
                   loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False,
                   labelcolor=INK_2, ncol=2, fontsize=9, borderaxespad=0.2)
    ax2.axhline(0, color=BASELINE, linewidth=1)
    ax2.set_ylabel("Each trade")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax2.margins(y=0.25)
    ax1.margins(x=0.08)

    fig.text(0.07, 0.965, title, fontsize=14, fontweight="bold", color=INK, ha="left")
    fig.text(0.07, 0.928, subtitle, fontsize=10.5, color=INK_2, ha="left")
    fig.text(0.07, 0.015, "Simulated paper trades - nothing was bought or sold. Net = after est. fees + slippage.",
             fontsize=8, color=MUTED, ha="left")
    fig.subplots_adjust(top=0.88, bottom=0.09, left=0.12, right=0.9)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
