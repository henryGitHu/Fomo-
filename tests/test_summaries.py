"""Scheduled summaries: hourly digest, P&L chart, end-of-day, instant-alert switch."""
from __future__ import annotations

import time
from datetime import datetime

import httpx
import pytest

from scanner import summaries as summaries_mod
from scanner.alerts import AlertManager, AlertSender
from scanner.charts import render_pnl_chart
from scanner.config import ConfigError, load_config
from scanner.http import HttpClient
from scanner.storage import Storage
from scanner.summaries import KEY_DIGEST, Summaries
from scanner.tracker import HIT_SL, HIT_TP

from test_phase1 import ROOT, fast
from test_phase3 import CHAT, TOKEN, hot_snap, make_row
from test_phase4 import add_alert


@pytest.fixture
def cfg():
    c = fast(load_config(ROOT / "config.yaml", env_path=ROOT / "none.env"))
    c.secrets = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch, tmp_path):
    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)
    monkeypatch.setattr(summaries_mod, "CHART_PATH", tmp_path / "reports" / "latest-pnl.png")


class Recorder:
    def __init__(self):
        self.calls = []

    def client(self):
        def handler(request):
            ctype = request.headers.get("content-type", "")
            self.calls.append((str(request.url), ctype, request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})
        return HttpClient(max_retries=0, transport=httpx.MockTransport(handler))


def build(cfg, tmp_path, rec):
    storage = Storage(tmp_path / "s.db")
    return Summaries(cfg, storage, AlertSender(cfg, rec.client(), dry_run=False)), storage


def noon_today():
    return datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def seed_trades(storage, now):
    """Two trades that closed this morning: one win, one loss."""
    for i, (hit, net) in enumerate(((HIT_TP, 13.8), (HIT_SL, -9.2))):
        aid = add_alert(storage, now - 3 * 3600 + i * 600, addr=f"Tok{i}")
        storage.save_outcome(aid, hit=hit, hit_ts=now - 2 * 3600 + i * 600,
                             gross_return_pct=net + 1.2, net_return_pct=net, updated_ts=now)
    storage.commit()


def test_digest_lists_top_tokens(cfg, tmp_path):
    rec = Recorder()
    s, _ = build(cfg, tmp_path, rec)
    rows = [make_row(cfg, snap=hot_snap(token_address=f"M{i}", symbol=f"T{i}",
                                         price_change_h1=5 + i * 5)) for i in range(7)]
    text = s.digest_text(rows, time.time())
    assert "Top tokens right now" in text and "not alerts" in text
    assert text.count("score <b>") == cfg.summaries.digest_top_n
    assert "T6" in text and "chart" in text


def test_schedule_sends_once_per_period_and_survives_restart(cfg, tmp_path):
    cfg.summaries.pnl_daily_time = ""
    rec = Recorder()
    s, storage = build(cfg, tmp_path, rec)
    now = noon_today()
    assert s.maybe_send([], now) == ["digest", "pnl"]
    assert s.maybe_send([], now + 600) == []                      # nothing due 10 min later
    # "Restart": a new Summaries on the same database remembers what was sent.
    s2 = Summaries(cfg, storage, AlertSender(cfg, rec.client(), dry_run=False))
    assert s2.maybe_send([], now + 1200) == []
    assert s2.maybe_send([], now + 3601) == ["digest"]
    assert s2.maybe_send([], now + 2 * 3600 + 1) == ["digest", "pnl"]   # both due at 2h


def test_end_of_day_summary_once(cfg, tmp_path):
    cfg.summaries.digest = False
    cfg.summaries.pnl_daily_time = "21:00"
    rec = Recorder()
    s, storage = build(cfg, tmp_path, rec)
    evening = datetime.now().replace(hour=21, minute=5, second=0, microsecond=0).timestamp()
    storage.set_value("last_pnl_ts", str(evening - 600))           # regular chart sent recently
    assert s.maybe_send([], evening - 3600) == []                  # 20:05: not yet
    assert s.maybe_send([], evening) == ["daily"]
    assert s.maybe_send([], evening + 1800) == []                  # only once per day


def test_pnl_chart_sent_as_photo(cfg, tmp_path):
    rec = Recorder()
    s, storage = build(cfg, tmp_path, rec)
    now = noon_today()
    seed_trades(storage, now)
    assert s.send_pnl(now)
    url, ctype, body = rec.calls[-1]
    assert url.endswith("/sendPhoto") and ctype.startswith("multipart/form-data")
    assert b"\x89PNG" in body                                      # an actual image
    assert b"Net <b>+$2.30</b>" in body and b"1 wins / 1 losses" in body
    assert summaries_mod.CHART_PATH.exists()


def test_pnl_without_trades_sends_text(cfg, tmp_path):
    rec = Recorder()
    s, _ = build(cfg, tmp_path, rec)
    assert s.send_pnl(noon_today())
    url, ctype, body = rec.calls[-1]
    assert url.endswith("/sendMessage") and b"No paper trades have finished yet today" in body


def test_chart_renders_png():
    t0 = noon_today()
    png = render_pnl_chart([(t0, 6.9, "A"), (t0 + 900, -4.6, "B"), (t0 + 1800, 6.9, "C")],
                           "Paper trading - test", "Net +$9.20")
    assert png and png[:4] == b"\x89PNG" and len(png) > 10_000


def test_instant_alerts_can_be_turned_off(cfg, tmp_path):
    cfg.alerts.instant = False
    rec = Recorder()
    m = AlertManager(cfg, Storage(tmp_path / "a.db"), rec.client(), dry_run=False)
    assert m.process([make_row(cfg)]) == [] and rec.calls == []


def test_daily_time_validation(tmp_path):
    text = (ROOT / "config.yaml").read_text().replace('pnl_daily_time: "21:00"', 'pnl_daily_time: "9pm"')
    p = tmp_path / "config.yaml"
    p.write_text(text)
    with pytest.raises(ConfigError, match="pnl_daily_time"):
        load_config(p, env_path=tmp_path / "none.env")
