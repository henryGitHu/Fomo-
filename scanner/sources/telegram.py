"""Telegram public channel mention tracking via the official client API (Telethon).

Logs in as YOUR Telegram account (api_id / api_hash from my.telegram.org) and
only READS public channels/groups listed in config.yaml. It never posts.

    python -m scanner.sources.telegram --login    one-time login + channel check

The login session is stored in data/telegram.session - treat it like a
password (anyone with that file can use your Telegram account).

Rate limits: Telegram tells clients to wait when they ask too often
(FloodWait). We poll gently (default every 2 minutes) and back off for the
time Telegram asks for.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from ..config import PROJECT_ROOT, Config, ConfigError, load_config
from ..social import MATCH_ADDRESS, extract_mentions, text_hash
from ..storage import Storage

log = logging.getLogger(__name__)

SESSION_PATH = PROJECT_ROOT / "data" / "telegram"   # Telethon adds ".session"
KEY_LAST_ID = "tg_last_id:"
KEY_LAST_POLL = "tg_last_poll"
FIRST_RUN_LOOKBACK = 6 * 3600     # on first sight of a channel, read this far back


def _make_client(cfg: Config):
    from telethon.sync import TelegramClient

    api_id = cfg.secrets.get("TELEGRAM_API_ID")
    api_hash = cfg.secrets.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ConfigError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env "
                          "(get them at https://my.telegram.org - see README).")
    try:
        api_id_int = int(str(api_id).strip())
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID in .env must be a number.") from exc
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Telethon backs off automatically for short FloodWaits (up to 60s).
    return TelegramClient(str(SESSION_PATH), api_id_int, str(api_hash).strip(),
                          flood_sleep_threshold=60)


class TelegramSource:
    def __init__(self, cfg: Config, storage: Storage, client_factory=_make_client):
        self.cfg = cfg
        self.storage = storage
        self._factory = client_factory
        self.client = None
        self.ready = False
        self._entities: dict[str, object] = {}
        self._backoff_until: dict[str, float] = {}
        self._warned: set[str] = set()
        self._ignore = {t.upper() for t in cfg.social.ignore_tickers}

    # ------------------------------------------------------------------ connect
    def connect(self) -> bool:
        """Connect without prompting. False (with a warning) if not logged in yet."""
        try:
            self.client = self._factory(self.cfg)
            self.client.connect()
            if not self.client.is_user_authorized():
                log.warning("Telegram channels: not logged in yet - double-click telegram-login.bat first.")
                self.ready = False
            else:
                self.ready = True
        except ConfigError as exc:
            log.warning("Telegram channels: %s", exc)
            self.ready = False
        except ImportError:
            log.warning("Telegram channels: the 'telethon' package isn't installed - run setup.bat again.")
            self.ready = False
        except Exception as exc:
            log.warning("Telegram channels: couldn't connect (%s)", exc)
            self.ready = False
        return self.ready

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------ polling
    def poll_due(self, now: float) -> bool:
        last = float(self.storage.get_value(KEY_LAST_POLL, "0") or 0)
        return now - last >= self.cfg.telegram_channels.poll_every_seconds

    def poll(self, now: float | None = None) -> int:
        """Read new messages from every channel and store mentions. Returns # stored."""
        now = now or time.time()
        if not self.ready or not self.poll_due(now):
            return 0
        total = 0
        for channel in self.cfg.telegram_channels.channels:
            if self._backoff_until.get(channel, 0) > now:
                continue
            try:
                total += self._poll_channel(channel, now)
            except Exception as exc:
                self._handle_error(channel, exc, now)
        self.storage.set_value(KEY_LAST_POLL, str(now))
        return total

    def _entity(self, channel: str):
        if channel not in self._entities:
            self._entities[channel] = self.client.get_entity(channel)
        return self._entities[channel]

    def _poll_channel(self, channel: str, now: float) -> int:
        key = KEY_LAST_ID + channel.lower()
        last_id = int(self.storage.get_value(key, "0") or 0)
        entity = self._entity(channel)
        messages = self.client.get_messages(entity, limit=self.cfg.telegram_channels.messages_per_poll,
                                            min_id=last_id)
        rows, newest = [], last_id
        for msg in messages or []:
            newest = max(newest, int(getattr(msg, "id", 0) or 0))
            date = getattr(msg, "date", None)
            ts = date.timestamp() if date else now
            if last_id == 0 and ts < now - FIRST_RUN_LOOKBACK:
                continue
            rows += self.message_to_mentions(channel, msg, ts)
        if newest > last_id:
            self.storage.set_value(key, str(newest))
        self.storage.add_mentions(rows)
        self._warned.discard(channel)
        return len(rows)

    def message_to_mentions(self, channel: str, msg, ts: float) -> list[dict]:
        text = getattr(msg, "message", None) or getattr(msg, "raw_text", None) or ""
        # Hidden links ("click here" -> URL) carry contract addresses too.
        urls = [getattr(e, "url", "") for e in (getattr(msg, "entities", None) or []) if getattr(e, "url", None)]
        full = " ".join([text] + urls)
        found = extract_mentions(full, self._ignore)
        if not found:
            return []
        sender = getattr(msg, "sender_id", None)
        author = f"{channel}:{sender}" if sender else channel
        h = text_hash(text)
        link = f"https://t.me/{channel}/{getattr(msg, 'id', '')}"
        out = []
        for m in found:
            chain = "Solana" if m.chain_hint == "solana" else None   # EVM: matched on any EVM chain
            out.append({"ts": ts, "source": "telegram", "channel": channel, "author": author,
                        "author_age_days": None,     # Telegram doesn't expose account age
                        "chain": chain,
                        "token_address": m.value if m.match_type == MATCH_ADDRESS else None,
                        "ticker": m.value if m.match_type != MATCH_ADDRESS else None,
                        "match_type": m.match_type, "text_hash": h, "url": link})
        return out

    def _handle_error(self, channel: str, exc: Exception, now: float) -> None:
        name = type(exc).__name__
        seconds = getattr(exc, "seconds", None)
        if seconds:                                   # FloodWaitError: Telegram asked us to wait
            self._backoff_until[channel] = now + float(seconds) + 5
            log.info("Telegram asked to slow down for %s; waiting %ss", channel, seconds)
            return
        self._backoff_until[channel] = now + 600       # don't hammer a broken channel
        if channel not in self._warned:
            if name in ("UsernameNotOccupiedError", "UsernameInvalidError", "ValueError"):
                why = "no public channel with that name"
            elif name in ("ChannelPrivateError", "ChannelInvalidError"):
                why = "the channel is private or you can't access it"
            else:
                why = f"{name}: {exc}"
            log.warning("Telegram channel '%s' skipped (%s). Check the name in config.yaml.", channel, why)
            self._warned.add(channel)


# --------------------------------------------------------------------------
# one-time login (telegram-login.bat)
# --------------------------------------------------------------------------

def login(cfg: Config) -> int:
    from ..display import console

    try:
        client = _make_client(cfg)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    except ImportError:
        console.print("[red]The 'telethon' package isn't installed - run setup.bat again.[/red]")
        return 2
    console.print("Logging in to Telegram. Enter your phone number with country code "
                  "(e.g. +15551234567). Telegram will send you a login code in the Telegram app.")
    console.print("[dim]This tool only READS channels; it never posts as you.[/dim]")
    client.start()   # prompts for phone, code and (if set) your 2-step password
    me = client.get_me()
    console.print(f"[green]Logged in as {getattr(me, 'first_name', '') or 'you'}.[/green]")
    channels = cfg.telegram_channels.channels
    if not channels:
        console.print("[yellow]No channels listed yet - add some under telegram_channels.channels "
                      "in config.yaml.[/yellow]")
    for ch in channels:
        try:
            entity = client.get_entity(ch)
            msgs = client.get_messages(entity, limit=1)
            when = msgs[0].date.astimezone().strftime("%Y-%m-%d %H:%M") if msgs else "no messages"
            console.print(f"  [green]OK[/green]  {ch} - latest message {when}")
        except Exception as exc:
            console.print(f"  [red]PROBLEM[/red]  {ch} - {type(exc).__name__}: {exc}")
    if not cfg.telegram_channels.enabled:
        console.print("Now set telegram_channels.enabled: true in config.yaml and restart run.bat.")
    client.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram channel reader")
    parser.add_argument("--login", action="store_true", help="log in once and check channels")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"SETTINGS PROBLEM: {exc}", file=sys.stderr)
        return 2
    if args.login:
        return login(cfg)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
