"""Alerts: decide what to alert on, build the message, and deliver it.

Delivery:
  * Telegram bot (primary) - https://api.telegram.org/bot<token>/sendMessage
  * Discord webhook (optional) - POST {"content": ...} to the webhook URL
  * --dry-run prints alerts to the console instead of sending them.

Every alert is saved in the database (with the suggested trade) so phase 4
can paper-trade it. Nothing here ever places a trade.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass

from .config import Config, TradeSettings
from .http import HttpClient
from .models import ScanRow, Snapshot
from .safety import FAIL, PASS, STATUS_FAIL, STATUS_PASS, STATUS_UNVERIFIED, UNKNOWN
from .storage import Storage

log = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
BUCKET_TELEGRAM = "telegram"
BUCKET_DISCORD = "discord"
TELEGRAM_LIMIT = 4096
DISCORD_LIMIT = 2000


# --------------------------------------------------------------------------
# suggested trade + cost estimate
# --------------------------------------------------------------------------

@dataclass
class TradeSuggestion:
    entry: float
    take_profit: float
    stop_loss: float
    position_usd: float
    fee_pct: float
    slippage_pct: float
    cost_pct: float          # fees + slippage, round trip
    costs_eat_target: bool   # costs above the configured share of the TP gain


def estimate_slippage_pct(position_usd: float, liquidity_usd: float | None) -> float:
    """Rough round-trip price impact for a constant-product pool.

    Buying moves the price by about (size / tokens-side depth); a pool's USD
    liquidity is split about half and half, so one side ~= 2 * size / liquidity.
    Selling costs about the same again."""
    if not liquidity_usd or liquidity_usd <= 0:
        return 100.0
    return min(100.0, 2 * (2 * position_usd / liquidity_usd) * 100)


def suggest_trade(snap: Snapshot, t: TradeSettings) -> TradeSuggestion | None:
    if not snap.price_usd:
        return None
    slip = estimate_slippage_pct(t.position_usd, snap.liquidity_usd)
    cost = t.round_trip_fee_pct + slip
    return TradeSuggestion(
        entry=snap.price_usd,
        take_profit=snap.price_usd * (1 + t.take_profit_pct / 100),
        stop_loss=snap.price_usd * (1 - t.stop_loss_pct / 100),
        position_usd=t.position_usd,
        fee_pct=t.round_trip_fee_pct,
        slippage_pct=slip,
        cost_pct=cost,
        costs_eat_target=cost > t.take_profit_pct * t.warn_if_costs_exceed_pct_of_tp / 100,
    )


# --------------------------------------------------------------------------
# message formatting (Telegram HTML; converted to plain text elsewhere)
# --------------------------------------------------------------------------

def _money(v: float | None) -> str:
    if v is None:
        return "?"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suffix}"
    return f"${v:,.0f}"


def _price(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1:
        return f"${v:,.4f}"
    import math
    decimals = min(18, -math.floor(math.log10(abs(v))) + 3) if v else 2
    return f"${v:.{decimals}f}"


def _pct(v: float | None) -> str:
    return "?" if v is None else f"{v:+.1f}%"


SAFETY_ICON = {PASS: "✅", FAIL: "❌", UNKNOWN: "❔"}
STATUS_TEXT = {STATUS_PASS: "PASSED", STATUS_UNVERIFIED: "UNVERIFIED ⚠️", STATUS_FAIL: "FAILED"}
SIGNAL_TEXT = {"volume_spike": "volume spike", "buy_pressure": "buyers dominating",
               "price_uptrend": "steady uptrend", "giant_candle": "one giant candle (caution)"}


def format_alert(row: ScanRow, trade: TradeSuggestion | None, cfg: Config) -> str:
    s, mom, comp, saf = row.snap, row.momentum, row.composite, row.safety
    e = html.escape
    name = e(s.symbol or s.token_address[:8])
    lines = [
        f"🚨 <b>{name}</b> ({e(s.name)}) · {e(s.chain)}",
        f"Score <b>{comp.score:.0f}/100</b> · Risk <b>{comp.risk}</b> · Safety {STATUS_TEXT.get(saf.status, saf.status)}",
        f"<code>{e(s.token_address)}</code>",
        "",
        f"Price {_price(s.price_usd)} | 5m {_pct(s.price_change_m5)} | 1h {_pct(s.price_change_h1)}",
    ]
    vol_x = f" ({mom.volume_ratio:.1f}x normal)" if mom.volume_ratio else ""
    lines.append(f"Volume 5m {_money(s.volume_m5)} | 1h {_money(s.volume_h1)}{vol_x}")
    age = s.age_minutes()
    age_txt = "?" if age is None else (f"{age:.0f}m" if age < 120 else
                                        f"{age / 60:.0f}h" if age < 2880 else f"{age / 1440:.0f}d")
    lines.append(f"Liquidity {_money(s.liquidity_usd)} | MCap {_money(s.market_cap_usd or s.fdv_usd)} | Age {age_txt}")
    if s.buys_m5 is not None and s.sells_m5 is not None:
        ratio = f" ({mom.buy_ratio_m5 * 100:.0f}% buys)" if mom.buy_ratio_m5 is not None else ""
        lines.append(f"Buys/sells 5m: {s.buys_m5}/{s.sells_m5}{ratio}")
    social = comp.parts.get("social")
    lines.append("Social: " + ("not tracked yet" if social is None else f"{social:.0f}/100"))
    if mom.signals:
        lines.append("Signals: " + ", ".join(SIGNAL_TEXT.get(x, x) for x in mom.signals))

    lines += ["", "<b>Safety</b>"]
    for c in saf.checks:
        lines.append(f"{SAFETY_ICON.get(c.status, '•')} {e(c.detail)}")

    parts = comp.parts
    breakdown = " · ".join(f"{k} {'n/a' if v is None else f'{v:.0f}'}" for k, v in parts.items())
    lines += ["", f"<b>Score breakdown</b>: {breakdown}"]
    if comp.risk_reasons:
        lines.append("Risk factors: " + ", ".join(comp.risk_reasons))

    if trade:
        t = cfg.trade
        lines += [
            "",
            "💡 <b>Suggested trade</b> <i>(a suggestion only - you decide)</i>",
            f"Entry ~{_price(trade.entry)}",
            f"Take profit {_price(trade.take_profit)} (+{t.take_profit_pct:g}%)",
            f"Stop loss {_price(trade.stop_loss)} (-{t.stop_loss_pct:g}%)",
            f"Size ${trade.position_usd:,.0f}",
            f"Est. costs ~{trade.cost_pct:.1f}% round trip "
            f"(fees {trade.fee_pct:g}% + slippage ~{trade.slippage_pct:.1f}%) vs +{t.take_profit_pct:g}% target",
        ]
        if trade.costs_eat_target:
            lines.append("⚠️ <b>Costs would eat most of the profit target</b> - consider skipping or a smaller size")

    if s.url:
        lines += ["", f'<a href="{e(s.url, quote=True)}">Chart on DexScreener</a>']
    return "\n".join(lines)


def to_plain(text_html: str) -> str:
    text = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2: \1", text_html)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------

class AlertSender:
    def __init__(self, cfg: Config, http: HttpClient, dry_run: bool):
        self.cfg = cfg
        self.http = http
        self.dry_run = dry_run
        http.add_bucket(BUCKET_TELEGRAM, 20)
        http.add_bucket(BUCKET_DISCORD, 20)
        sec = cfg.secrets
        self.telegram_ready = bool(cfg.alerts.telegram and sec.get("TELEGRAM_BOT_TOKEN")
                                   and sec.get("TELEGRAM_CHAT_ID"))
        self.discord_ready = bool(cfg.alerts.discord and sec.get("DISCORD_WEBHOOK_URL"))

    def setup_problems(self) -> list[str]:
        sec, a = self.cfg.secrets, self.cfg.alerts
        problems = []
        if a.telegram and not (sec.get("TELEGRAM_BOT_TOKEN") and sec.get("TELEGRAM_CHAT_ID")):
            problems.append("Telegram is switched on but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
                            "are missing from .env")
        if a.discord and not sec.get("DISCORD_WEBHOOK_URL"):
            problems.append("Discord is switched on but DISCORD_WEBHOOK_URL is missing from .env")
        if not a.telegram and not a.discord:
            problems.append("Both telegram and discord are false in config.yaml")
        return problems

    def send(self, text_html: str) -> bool:
        """Deliver to every configured channel. True if at least one worked."""
        if self.dry_run:
            from .display import console
            console.rule("[bold yellow]ALERT (dry run - not sent)[/bold yellow]")
            console.print(to_plain(text_html), markup=False, highlight=False)
            console.rule()
            return False
        ok = False
        if self.telegram_ready:
            ok |= self._telegram(text_html) is None
        if self.discord_ready:
            ok |= self._discord(to_plain(text_html)) is None
        if not (self.telegram_ready or self.discord_ready):
            from .display import console
            console.rule("[bold yellow]ALERT (no delivery set up - shown here)[/bold yellow]")
            console.print(to_plain(text_html), markup=False, highlight=False)
            console.rule()
        return ok

    def _telegram(self, text_html: str) -> str | None:
        """Returns None on success, else a plain-English error."""
        sec = self.cfg.secrets
        url = TELEGRAM_URL.format(token=sec["TELEGRAM_BOT_TOKEN"].strip())
        body = {"chat_id": sec["TELEGRAM_CHAT_ID"].strip(), "text": text_html[:TELEGRAM_LIMIT],
                "parse_mode": "HTML", "disable_web_page_preview": True}
        status, data = self.http.post_json_status(url, body, bucket=BUCKET_TELEGRAM,
                                                  expected=(400, 401, 403, 404))
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            return None
        desc = str((data or {}).get("description", "")) if isinstance(data, dict) else ""
        if status in (401, 404):
            err = "Telegram rejected the bot token - check TELEGRAM_BOT_TOKEN in .env"
        elif status == 400 and "chat not found" in desc.lower():
            err = ("Telegram says the chat wasn't found - check TELEGRAM_CHAT_ID in .env, "
                   "and make sure you pressed Start in your bot's chat")
        elif status == 403:
            err = "Telegram says the bot was blocked - open your bot's chat and press Start/Unblock"
        elif status is None:
            err = "Couldn't reach Telegram (network problem)"
        else:
            err = f"Telegram error {status}: {desc or 'unknown'}"
        log.warning(err)
        return err

    def _discord(self, text: str) -> str | None:
        status, _ = self.http.post_json_status(self.cfg.secrets["DISCORD_WEBHOOK_URL"].strip(),
                                               {"content": text[:DISCORD_LIMIT]},
                                               bucket=BUCKET_DISCORD, expected=(400, 401, 404))
        if status in (200, 204):
            return None
        err = ("Discord rejected the webhook - check DISCORD_WEBHOOK_URL in .env"
               if status in (401, 404) else f"Discord error {status}")
        log.warning(err)
        return err

    def send_test(self) -> list[str]:
        """Send a test message; returns a list of result lines for the user."""
        msg = ("✅ <b>Crypto Momentum Scanner</b> test message.\n"
               "If you can read this, alerts will reach you here. "
               "This tool never trades - it only sends suggestions.")
        results = [f"Problem: {p}" for p in self.setup_problems()]
        if self.telegram_ready:
            err = self._telegram(msg)
            results.append("Telegram: test message sent - check your phone!" if err is None
                           else f"Telegram: FAILED - {err}")
        if self.discord_ready:
            err = self._discord(to_plain(msg))
            results.append("Discord: test message sent." if err is None else f"Discord: FAILED - {err}")
        return results


# --------------------------------------------------------------------------
# deciding what to alert on
# --------------------------------------------------------------------------

class AlertManager:
    def __init__(self, cfg: Config, storage: Storage, http: HttpClient, dry_run: bool,
                 safety_checker=None):
        self.cfg = cfg
        self.storage = storage
        self.sender = AlertSender(cfg, http, dry_run)
        self.dry_run = dry_run
        self.safety_checker = safety_checker

    def eligible(self, row: ScanRow, now: float) -> str | None:
        """None if the row should alert, else the reason it shouldn't."""
        a = self.cfg.alerts
        if not row.filters.passed:
            return "basic filters"
        if row.safety is None:
            return "not safety-checked yet"
        if row.safety.status == STATUS_FAIL:
            return "failed safety"
        if row.safety.status == STATUS_UNVERIFIED and not a.send_unverified:
            return "unverified"
        if self.safety_checker and not self.safety_checker.is_fresh(row.safety, now):
            return "safety result is stale (re-check pending)"
        if row.composite is None or row.composite.score < a.score_threshold:
            return "score below threshold"
        last = self.storage.last_alert(row.snap.chain, row.snap.token_address)
        if last is not None and now - last["ts"] < a.cooldown_minutes * 60:
            if row.composite.score < (last["score"] or 0) + a.realert_score_jump:
                return "cooldown"
        return None

    def process(self, rows: list[ScanRow], now: float | None = None) -> list[int]:
        now = now or time.time()
        ready = [r for r in rows if self.eligible(r, now) is None]
        ready.sort(key=lambda r: r.composite.score, reverse=True)
        sent_ids = []
        for row in ready[: self.cfg.alerts.max_alerts_per_scan]:
            trade = suggest_trade(row.snap, self.cfg.trade)
            text = format_alert(row, trade, self.cfg)
            signals = list(row.momentum.signals) + sorted(row.feeds) + [f"safety_{row.safety.status.lower()}"]
            alert_id = self.storage.add_alert(
                ts=now, chain=row.snap.chain, token_address=row.snap.token_address,
                symbol=row.snap.symbol, score=row.composite.score,
                score_breakdown=json.dumps(row.composite.parts), signals=json.dumps(signals),
                safety_status=row.safety.status, safety_details=row.safety.to_json(),
                price_usd=row.snap.price_usd,
                take_profit_usd=trade.take_profit if trade else None,
                stop_loss_usd=trade.stop_loss if trade else None,
                position_usd=trade.position_usd if trade else None,
                est_cost_pct=trade.cost_pct if trade else None,
                message=text, pair_address=row.snap.pair_address,
                liquidity_usd=row.snap.liquidity_usd, risk=row.composite.risk,
                dry_run=int(self.dry_run),
            )
            if self.sender.send(text):
                self.storage.mark_delivered(alert_id)
            log.info("ALERT %s %s score=%.0f safety=%s (id %d)", row.snap.chain, row.snap.symbol,
                     row.composite.score, row.safety.status, alert_id)
            sent_ids.append(alert_id)
        return sent_ids
