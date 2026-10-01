"""Scheduled summaries sent to Telegram/Discord.

  * Digest (default hourly): the best tokens right now, even if none crossed
    the alert threshold.
  * P&L chart (default every 2 hours, plus end of day): today's paper-trade
    results as an image - running profit/loss and each trade's win or loss.

Send times are stored in the database, so restarting the scanner doesn't
resend everything. "Today" follows the PC's clock (midnight to midnight).
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime

from .alerts import AlertSender, _money, _pct
from .charts import render_pnl_chart
from .config import PROJECT_ROOT, Config
from .models import ScanRow
from .safety import STATUS_FAIL, STATUS_PASS
from .storage import Storage
from .tracker import FINISHED, HIT_OPEN

log = logging.getLogger(__name__)

KEY_DIGEST = "last_digest_ts"
KEY_PNL = "last_pnl_ts"
KEY_DAILY = "last_daily_date"
CHART_PATH = PROJECT_ROOT / "reports" / "latest-pnl.png"


def _midnight(now: float) -> float:
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


class Summaries:
    def __init__(self, cfg: Config, storage: Storage, sender: AlertSender):
        self.cfg = cfg
        self.storage = storage
        self.sender = sender

    # ------------------------------------------------------------------ scheduling
    def _due(self, key: str, every_seconds: float, now: float) -> bool:
        last = float(self.storage.get_value(key, "0") or 0)
        return now - last >= every_seconds

    def maybe_send(self, rows: list[ScanRow], now: float | None = None) -> list[str]:
        """Send whatever is due. Returns the names of what was sent (for logs/tests)."""
        now = now or time.time()
        s = self.cfg.summaries
        sent = []
        if s.digest and self._due(KEY_DIGEST, s.digest_every_minutes * 60, now):
            self.sender.send(self.digest_text(rows, now), label="HOURLY DIGEST")
            self.storage.set_value(KEY_DIGEST, str(now))
            sent.append("digest")
        if s.pnl_chart and s.pnl_daily_time:
            today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            hh, mm = (int(x) for x in s.pnl_daily_time.split(":"))
            due_at = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0).timestamp()
            if now >= due_at and self.storage.get_value(KEY_DAILY) != today:
                self.send_pnl(now, end_of_day=True)
                self.storage.set_value(KEY_DAILY, today)
                self.storage.set_value(KEY_PNL, str(now))
                sent.append("daily")
        if s.pnl_chart and self._due(KEY_PNL, s.pnl_every_hours * 3600, now):
            self.send_pnl(now)
            self.storage.set_value(KEY_PNL, str(now))
            sent.append("pnl")
        return sent

    # ------------------------------------------------------------------ digest
    def digest_text(self, rows: list[ScanRow], now: float) -> str:
        e = html.escape
        n = self.cfg.summaries.digest_top_n
        good = [r for r in rows if r.filters.passed and r.safety is not None
                and r.safety.status != STATUS_FAIL and r.composite is not None]
        good.sort(key=lambda r: r.composite.score, reverse=True)
        lines = [f"🕐 <b>Top tokens right now</b> · {datetime.fromtimestamp(now):%H:%M}",
                 f"<i>Alert threshold is {self.cfg.alerts.score_threshold:.0f}. "
                 f"These are watch-list ideas, not alerts.</i>", ""]
        if not good:
            lines.append("No tokens passed the filters and safety checks in the latest scan.")
        for i, r in enumerate(good[:n], start=1):
            s = r.snap
            safety = "✅" if r.safety.status == STATUS_PASS else "⚠️ unverified"
            name = e(s.symbol or s.token_address[:8])
            link = f' · <a href="{e(s.url, quote=True)}">chart</a>' if s.url else ""
            lines.append(f"{i}. <b>{name}</b> ({e(s.chain)}) · score <b>{r.composite.score:.0f}</b> · {safety}")
            lines.append(f"    1h {_pct(s.price_change_h1)} · 5m {_pct(s.price_change_m5)} · "
                         f"liq {_money(s.liquidity_usd)} · risk {r.composite.risk}{link}")
        alerts = self.storage.alerts_since(now - self.cfg.summaries.digest_every_minutes * 60)
        open_trades = sum(1 for r in self.storage.alerts_with_outcomes(now - 5 * 3600)
                          if r["hit"] in (None, HIT_OPEN))
        lines += ["", f"Alerts in this period: {alerts} · Paper trades still open: {open_trades}"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ P&L chart
    def pnl_today(self, now: float):
        """Finished paper trades that closed today, oldest first."""
        start = _midnight(now)
        rows = self.storage.alerts_with_outcomes(start - 6 * 3600)
        done = [r for r in rows if r["hit"] in FINISHED and r["net_return_pct"] is not None
                and r["hit_ts"] and start <= r["hit_ts"] <= now]
        done.sort(key=lambda r: r["hit_ts"])
        open_ = [r for r in rows if r["hit"] in (None, HIT_OPEN)]
        return done, open_

    def send_pnl(self, now: float | None = None, end_of_day: bool = False) -> bool:
        now = now or time.time()
        done, open_ = self.pnl_today(now)
        trades = [(r["hit_ts"], (r["net_return_pct"] / 100) * (r["position_usd"] or 0),
                   r["symbol"] or "?") for r in done]
        total = sum(v for _, v, _ in trades)
        wins = sum(1 for _, v, _ in trades if v > 0)
        losses = len(trades) - wins
        when = "end of day" if end_of_day else f"today so far ({datetime.fromtimestamp(now):%H:%M})"
        all_rows = [r for r in self.storage.alerts_with_outcomes(0)
                    if r["hit"] in FINISHED and r["net_return_pct"] is not None]
        all_total = sum((r["net_return_pct"] / 100) * (r["position_usd"] or 0) for r in all_rows)

        caption = [f"📊 <b>Paper trading - {when}</b>"]
        if trades:
            caption.append(f"Net <b>{_usd(total)}</b> · {len(trades)} trades · {wins} wins / {losses} losses"
                           f" · win rate {wins / len(trades) * 100:.0f}%")
            best = max(trades, key=lambda t: t[1])
            worst = min(trades, key=lambda t: t[1])
            caption.append(f"Best {html.escape(best[2])} {_usd(best[1])} · Worst {html.escape(worst[2])} {_usd(worst[1])}")
        else:
            caption.append("No paper trades have finished yet today.")
        caption.append(f"Still open: {len(open_)} · All-time: {_usd(all_total)} over {len(all_rows)} trades")
        caption.append("<i>Simulated - nothing was bought or sold.</i>")
        text = "\n".join(caption)

        png = None
        if trades:
            subtitle = (f"Net {_usd(total)}  ·  {len(trades)} trades  ·  {wins} wins / {losses} losses")
            png = render_pnl_chart(trades, f"Paper trading - {when}", subtitle)
        return self.sender.send_photo(png, text, save_path=CHART_PATH)
