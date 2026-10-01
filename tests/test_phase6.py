"""Phase 6 tests: mention extraction, shill filtering, social score, Telegram reader."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scanner.config import ConfigError, load_config
from scanner.social import MATCH_ADDRESS, MATCH_TICKER, SocialIndex, extract_mentions, text_hash
from scanner.sources.telegram import KEY_LAST_ID, TelegramSource
from scanner.storage import Storage

from test_phase1 import ROOT

SOL = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
EVM = "0xAbC0000000000000000000000000000000000001"
NOW = 1_800_000_000.0


@pytest.fixture
def cfg():
    c = load_config(ROOT / "config.yaml", env_path=ROOT / "none.env")
    c.telegram_channels.enabled = True
    c.telegram_channels.channels = ["alpha", "beta"]
    return c


# ------------------------------------------------------------------ extraction

def test_extracts_addresses_and_skips_tickers_when_address_present():
    found = extract_mentions(f"$ROCKET is live {SOL} chart https://dexscreener.com/base/{EVM}")
    assert {(m.match_type, m.value) for m in found} == {(MATCH_ADDRESS, SOL), (MATCH_ADDRESS, EVM.lower())}


def test_extracts_cashtags_only_and_ignores_common():
    found = extract_mentions("$PEPE, $btc and ROCKET and $1000 and price$5", {"BTC"})
    assert [(m.match_type, m.value) for m in found] == [(MATCH_TICKER, "PEPE")]


def test_rejects_long_words_as_solana_addresses():
    assert extract_mentions("Supercalifragilisticexpialidociousxx is not an address") == []


def test_text_hash_ignores_links_case_and_spacing():
    assert text_hash("BUY $X now  https://a.com/1") == text_hash("buy $x NOW https://b.com/2")


# ------------------------------------------------------------------ scoring

def mention(ts, author, channel="alpha", addr=SOL, ticker=None, text="t", age=None):
    return {"ts": ts, "author": author, "channel": channel, "author_age_days": age,
            "token_address": addr, "ticker": ticker,
            "match_type": MATCH_ADDRESS if addr else MATCH_TICKER, "text_hash": text_hash(text)}


def test_acceleration_and_breadth_score_high(cfg):
    rows = [mention(NOW - 60 * i, f"user{i}", channel=["alpha", "beta", "gamma"][i % 3], text=f"msg {i}")
            for i in range(1, 9)]                                  # 8 fresh mentions, 8 authors
    res = SocialIndex(rows, NOW, cfg.social).score({SOL}, "ROCKET")
    assert res.score >= 90 and res.sources == 8 and res.match_type == MATCH_ADDRESS


def test_steady_chatter_scores_lower_than_a_spike(cfg):
    steady = [mention(NOW - 1800 * i - 60, f"u{i}", text=f"m{i}") for i in range(0, 12)]   # every 30 min
    spike = [mention(NOW - 60 * i, f"u{i}", text=f"m{i}") for i in range(1, 7)]
    s1 = SocialIndex(steady, NOW, cfg.social).score({SOL}, "X").score
    s2 = SocialIndex(spike, NOW, cfg.social).score({SOL}, "X").score
    assert s2 > s1


def test_shill_filter_copy_paste_and_repeat_authors(cfg):
    spam = [mention(NOW - 60 * i, f"bot{i}", text="BUY NOW 100x") for i in range(1, 20)]   # same text
    repeat = [mention(NOW - 60 * i, "oneguy", text=f"different {i}") for i in range(1, 20)]  # one author
    assert SocialIndex(spam, NOW, cfg.social).score({SOL}, "X").recent == 1
    assert SocialIndex(repeat, NOW, cfg.social).score({SOL}, "X").recent == 1


def test_ticker_only_counts_less_and_new_accounts_downweighted(cfg):
    tick = [mention(NOW - 60, "a", addr=None, ticker="ROCKET")]
    res = SocialIndex(tick, NOW, cfg.social).score({SOL}, "rocket")
    assert res.recent == pytest.approx(cfg.social.ticker_only_weight) and res.match_type == MATCH_TICKER
    young = [mention(NOW - 60, "a", age=2)]
    assert SocialIndex(young, NOW, cfg.social).score({SOL}, "X").recent == pytest.approx(cfg.social.new_account_weight)


def test_no_mentions_is_none_and_ignored_tickers_dont_match(cfg):
    idx = SocialIndex([mention(NOW - 60, "a", addr=None, ticker="SOL")], NOW, cfg.social)
    assert idx.score({EVM.lower()}, "SOL") is None


def test_evm_match_is_case_insensitive_and_pair_address_works(cfg):
    idx = SocialIndex([mention(NOW - 60, "a", addr="0xpair000000000000000000000000000000000001")], NOW, cfg.social)
    assert idx.score({"0xPAIR000000000000000000000000000000000001"}, "X") is not None


# ------------------------------------------------------------------ Telegram reader

class FakeClient:
    def __init__(self, messages, authorized=True):
        self.messages = messages
        self.authorized = authorized
        self.calls = []

    def connect(self):
        pass

    def is_user_authorized(self):
        return self.authorized

    def get_entity(self, name):
        if name == "missing":
            raise type("UsernameNotOccupiedError", (Exception,), {})("nope")
        return SimpleNamespace(username=name)

    def get_messages(self, entity, limit, min_id):
        self.calls.append((entity.username, min_id))
        return [m for m in self.messages.get(entity.username, []) if m.id > min_id]

    def disconnect(self):
        pass


def msg(id_, text, minutes_ago=1, sender=None, urls=()):
    return SimpleNamespace(id=id_, message=text, sender_id=sender,
                           date=datetime.fromtimestamp(time.time() - minutes_ago * 60, tz=timezone.utc),
                           entities=[SimpleNamespace(url=u) for u in urls])


def test_reader_stores_mentions_and_remembers_position(cfg, tmp_path):
    storage = Storage(tmp_path / "t.db")
    client = FakeClient({"alpha": [msg(5, f"new gem {SOL}"), msg(6, "hello no token"),
                                   msg(7, "check this", urls=[f"https://pump.fun/{SOL}"])],
                         "beta": [msg(1, "$PEPE pumping", sender=42), msg(2, "old", minutes_ago=600)]})
    src = TelegramSource(cfg, storage, client_factory=lambda c: client)
    assert src.connect()
    assert src.poll() == 3
    rows = storage.mentions_since(0)
    assert {r["match_type"] for r in rows} == {MATCH_ADDRESS, MATCH_TICKER}
    assert any(r["author"] == "beta:42" for r in rows)
    assert storage.get_value(KEY_LAST_ID + "alpha") == "7"
    # Next poll (after the interval) asks only for newer messages.
    src.poll(time.time() + 300)
    assert ("alpha", 7) in client.calls


def test_reader_skips_bad_channel_without_crashing(cfg, tmp_path, caplog):
    cfg.telegram_channels.channels = ["missing", "alpha"]
    storage = Storage(tmp_path / "t.db")
    src = TelegramSource(cfg, storage, client_factory=lambda c: FakeClient({"alpha": [msg(1, SOL)]}))
    src.connect()
    assert src.poll() == 1
    assert "missing" in caplog.text and "skipped" in caplog.text


def test_reader_not_logged_in_is_a_warning(cfg, tmp_path, caplog):
    src = TelegramSource(cfg, Storage(tmp_path / "t.db"),
                         client_factory=lambda c: FakeClient({}, authorized=False))
    assert not src.connect() and src.poll() == 0
    assert "telegram-login.bat" in caplog.text


def test_reader_missing_keys_is_a_warning(cfg, tmp_path, caplog):
    def factory(c):
        raise ConfigError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
    src = TelegramSource(cfg, Storage(tmp_path / "t.db"), client_factory=factory)
    assert not src.connect() and "TELEGRAM_API_ID" in caplog.text


def test_flood_wait_backs_off(cfg, tmp_path):
    class Flood(Exception):
        seconds = 120

    class FloodClient(FakeClient):
        def get_messages(self, entity, limit, min_id):
            raise Flood("wait")

    cfg.telegram_channels.channels = ["alpha"]
    src = TelegramSource(cfg, Storage(tmp_path / "t.db"), client_factory=lambda c: FloodClient({}))
    src.connect()
    now = time.time()
    src.poll(now)
    assert src._backoff_until["alpha"] >= now + 120


def test_channel_names_from_links_are_cleaned(tmp_path):
    text = (ROOT / "config.yaml").read_text().replace(
        "  channels: []", '  channels:\n    - https://t.me/SomeChannel\n    - "@other"\n    - t.me/third/')
    p = tmp_path / "config.yaml"
    p.write_text(text)
    assert load_config(p, env_path=tmp_path / "x.env").telegram_channels.channels == ["SomeChannel", "other", "third"]


def test_full_scan_uses_telegram_buzz(cfg, tmp_path, monkeypatch):
    import copy
    import httpx
    from scanner.http import HttpClient
    from scanner.main import Scanner
    from test_phase1 import fake_transport, fast
    from test_phase2 import GOOD_RUGCHECK

    monkeypatch.setattr("scanner.http.time.sleep", lambda s: None)
    cfg = fast(cfg)
    cfg.telegram_channels.channels = ["alpha", "beta", "gamma"]
    base = fake_transport([])

    def handler(request):
        url = str(request.url)
        if "rugcheck" in url:
            r = copy.deepcopy(GOOD_RUGCHECK)
            r["mint"] = url.split("/tokens/")[1].split("/")[0]
            return httpx.Response(200, json=r)
        if any(h in url for h in ("gopluslabs", "honeypot.is", "telegram.org")):
            return httpx.Response(503)
        return base.handle_request(request)

    chats = {ch: [msg(i, f"{ch} says {SOL} #{i}", minutes_ago=i, sender=100 + i) for i in range(1, 4)]
             for ch in ("alpha", "beta", "gamma")}
    storage = Storage(tmp_path / "t.db")
    scanner = Scanner(cfg, storage, HttpClient(max_retries=0, transport=httpx.MockTransport(handler)))
    scanner.telegram = TelegramSource(cfg, storage, client_factory=lambda c: FakeClient(chats))
    rows = {r.snap.symbol: r for r in scanner.scan_once()}
    rocket = rows["ROCKET"]
    assert rocket.social is not None and rocket.social.score >= 90 and rocket.social.sources == 9
    assert rocket.composite.parts["social"] == rocket.social.score
    assert rows["WHALE"].social is None and rows["WHALE"].composite.parts["social"] is None
