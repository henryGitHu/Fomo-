"""Load config.yaml + .env, validate, and expose typed settings.

Errors are raised as ConfigError with a plain-English message pointing at the
setting that needs fixing, so a non-programmer can correct config.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

# Secrets read from .env (used by later phases). Missing ones are simply None.
SECRET_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DISCORD_WEBHOOK_URL",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "GOPLUS_API_KEY",
)


class ConfigError(Exception):
    """A problem in config.yaml or .env, described in plain English."""


@dataclass
class Chain:
    name: str
    type: str  # "solana" or "evm"
    dexscreener_id: str
    geckoterminal_id: str
    enabled: bool = True
    chain_id: int | None = None  # EVM chain number (1 = Ethereum, 56 = BNB, ...)


@dataclass
class ScanSettings:
    interval_seconds: int
    max_candidates_per_chain: int
    candidate_memory_minutes: int


@dataclass
class DexScreenerSettings:
    enabled: bool
    use_boosts_latest: bool
    use_boosts_top: bool
    use_token_profiles: bool
    requests_per_minute_discovery: int
    requests_per_minute_pairs: int


@dataclass
class GeckoTerminalSettings:
    enabled: bool
    use_trending: bool
    use_new_pools: bool
    pages: int
    requests_per_minute: int


@dataclass
class Filters:
    min_liquidity_usd: float
    min_volume_1h_usd: float
    min_pair_age_minutes: float


@dataclass
class SolanaSafety:
    require_mint_authority_revoked: bool
    require_freeze_authority_revoked: bool
    min_lp_locked_pct: float
    fail_on_rugcheck_danger: bool
    rpc_url: str
    rugcheck_requests_per_minute: int
    rpc_requests_per_minute: int


@dataclass
class EvmSafety:
    max_buy_tax_pct: float
    max_sell_tax_pct: float
    fail_if_not_open_source: bool
    fail_on_owner_privileges: bool
    use_honeypot_is: bool
    goplus_requests_per_minute: int
    honeypot_is_requests_per_minute: int


@dataclass
class SafetySettings:
    enabled: bool
    max_checks_per_scan: int
    recheck_minutes_pass: int
    recheck_minutes_fail: int
    recheck_minutes_unverified: int
    hide_failed: bool
    strict_mode: bool
    max_top10_holders_pct: float
    solana: SolanaSafety
    evm: EvmSafety


@dataclass
class ScoringSettings:
    weights: dict[str, float]
    unverified_safety_multiplier: float


@dataclass
class AlertSettings:
    score_threshold: float
    cooldown_minutes: int
    realert_score_jump: float
    send_unverified: bool
    max_alerts_per_scan: int
    telegram: bool
    discord: bool


@dataclass
class TradeSettings:
    position_usd: float
    take_profit_pct: float
    stop_loss_pct: float
    round_trip_fee_pct: float
    warn_if_costs_exceed_pct_of_tp: float


@dataclass
class MomentumSettings:
    weights: dict[str, float]
    baseline_minutes: int
    volume_acceleration_full_score_ratio: float
    buy_ratio_full_score: float
    price_change_1h_full_score_pct: float
    giant_candle_pct_5m: float
    giant_candle_share_of_1h: float
    giant_candle_penalty: float


@dataclass
class DisplaySettings:
    top_n: int
    only_passing_filters: bool


@dataclass
class StorageSettings:
    database_path: Path
    keep_snapshots_days: int


@dataclass
class NetworkSettings:
    timeout_seconds: float
    max_retries: int


@dataclass
class LoggingSettings:
    log_file: Path
    max_file_mb: float
    backup_count: int
    level: str


@dataclass
class Config:
    chains: list[Chain]
    scan: ScanSettings
    dexscreener: DexScreenerSettings
    geckoterminal: GeckoTerminalSettings
    filters: Filters
    safety: SafetySettings
    momentum: MomentumSettings
    scoring: ScoringSettings
    alerts: AlertSettings
    trade: TradeSettings
    display: DisplaySettings
    storage: StorageSettings
    network: NetworkSettings
    logging: LoggingSettings
    secrets: dict[str, str | None] = field(default_factory=dict)

    @property
    def enabled_chains(self) -> list[Chain]:
        return [c for c in self.chains if c.enabled]

    def chain_by_dexscreener_id(self, ds_id: str) -> Chain | None:
        for c in self.enabled_chains:
            if c.dexscreener_id and c.dexscreener_id == ds_id:
                return c
        return None


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if value is None:
        raise ConfigError(f"config.yaml is missing the '{name}:' section.")
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}:' in config.yaml should contain indented settings.")
    return value


def _get(sec: dict, path: str, key: str, kind: type, *, minimum: float | None = None,
         maximum: float | None = None) -> Any:
    if key not in sec:
        raise ConfigError(f"config.yaml is missing the setting '{path}.{key}'.")
    value = sec[key]
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"'{path}.{key}' must be true or false (got {value!r}).")
        return value
    if kind in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"'{path}.{key}' must be a number (got {value!r}).")
        if kind is int and float(value) != int(value):
            raise ConfigError(f"'{path}.{key}' must be a whole number (got {value!r}).")
        value = kind(value)
        if minimum is not None and value < minimum:
            raise ConfigError(f"'{path}.{key}' must be at least {minimum} (got {value}).")
        if maximum is not None and value > maximum:
            raise ConfigError(f"'{path}.{key}' must be at most {maximum} (got {value}).")
        return value
    if kind is str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ConfigError(f"'{path}.{key}' must be text (got {value!r}).")
        return value.strip()
    raise TypeError(kind)


def _parse_chains(raw: dict) -> list[Chain]:
    items = raw.get("chains")
    if not isinstance(items, list) or not items:
        raise ConfigError("config.yaml needs a 'chains:' list with at least one chain.")
    chains: list[Chain] = []
    seen: set[str] = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Chain #{i} in config.yaml is not written correctly.")
        path = f"chains[{i}]"
        name = _get(item, path, "name", str)
        if not name:
            raise ConfigError(f"Chain #{i} in config.yaml has no name.")
        ctype = _get(item, path, "type", str).lower()
        if ctype not in ("solana", "evm"):
            raise ConfigError(f"Chain '{name}': type must be \"solana\" or \"evm\" (got {ctype!r}).")
        ds_id = str(item.get("dexscreener_id") or "").strip()
        gt_id = str(item.get("geckoterminal_id") or "").strip()
        if not ds_id and not gt_id:
            raise ConfigError(f"Chain '{name}' needs a dexscreener_id or a geckoterminal_id.")
        if name.lower() in seen:
            raise ConfigError(f"Chain '{name}' is listed twice in config.yaml.")
        seen.add(name.lower())
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"Chain '{name}': enabled must be true or false.")
        chain_id = item.get("chain_id")
        if chain_id in ("", None):
            chain_id = None
        elif isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id <= 0:
            raise ConfigError(f"Chain '{name}': chain_id must be a whole number like 1 or 56.")
        chains.append(Chain(name=name, type=ctype, dexscreener_id=ds_id,
                            geckoterminal_id=gt_id, enabled=enabled, chain_id=chain_id))
    if not any(c.enabled for c in chains):
        raise ConfigError("Every chain in config.yaml is disabled - enable at least one.")
    return chains


def _resolve(path_text: str) -> Path:
    p = Path(path_text)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(config_path: Path | str | None = None,
                env_path: Path | str | None = None) -> Config:
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH

    if not config_path.exists():
        raise ConfigError(f"Can't find the settings file: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark else ""
        raise ConfigError(f"config.yaml has a formatting mistake{where}. "
                          f"Check indentation and colons. ({exc})") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml is empty or not written correctly.")

    if env_path.exists():
        load_dotenv(env_path, override=False)
    secrets = {name: (os.getenv(name) or None) for name in SECRET_NAMES}

    scan = _section(raw, "scan")
    sources = _section(raw, "sources")
    ds = _section(sources, "dexscreener")
    gt = _section(sources, "geckoterminal")
    flt = _section(raw, "filters")
    saf = _section(raw, "safety")
    saf_sol = _section(saf, "solana")
    saf_evm = _section(saf, "evm")
    mom = _section(raw, "momentum")
    sco = _section(raw, "scoring")
    alr = _section(raw, "alerts")
    trd = _section(raw, "trade")
    disp = _section(raw, "display")
    sto = _section(raw, "storage")
    net = _section(raw, "network")
    log = _section(raw, "logging")

    weights_raw = mom.get("weights")
    expected_weights = ("volume_acceleration", "buy_pressure", "price_trend")
    if not isinstance(weights_raw, dict):
        raise ConfigError("'momentum.weights' must list: " + ", ".join(expected_weights))
    weights = {k: _get(weights_raw, "momentum.weights", k, float, minimum=0)
               for k in expected_weights}
    if sum(weights.values()) <= 0:
        raise ConfigError("'momentum.weights' can't all be zero.")

    sw_raw = sco.get("weights")
    if not isinstance(sw_raw, dict):
        raise ConfigError("'scoring.weights' must list: momentum, social, safety")
    score_weights = {k: _get(sw_raw, "scoring.weights", k, float, minimum=0)
                     for k in ("momentum", "social", "safety")}
    if score_weights["momentum"] + score_weights["safety"] <= 0:
        raise ConfigError("'scoring.weights' for momentum and safety can't both be zero.")

    level = _get(log, "logging", "level", str).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("'logging.level' must be DEBUG, INFO, WARNING or ERROR.")

    cfg = Config(
        chains=_parse_chains(raw),
        scan=ScanSettings(
            interval_seconds=_get(scan, "scan", "interval_seconds", int, minimum=10),
            max_candidates_per_chain=_get(scan, "scan", "max_candidates_per_chain", int, minimum=1, maximum=500),
            candidate_memory_minutes=_get(scan, "scan", "candidate_memory_minutes", int, minimum=0),
        ),
        dexscreener=DexScreenerSettings(
            enabled=_get(ds, "sources.dexscreener", "enabled", bool),
            use_boosts_latest=_get(ds, "sources.dexscreener", "use_boosts_latest", bool),
            use_boosts_top=_get(ds, "sources.dexscreener", "use_boosts_top", bool),
            use_token_profiles=_get(ds, "sources.dexscreener", "use_token_profiles", bool),
            requests_per_minute_discovery=_get(ds, "sources.dexscreener", "requests_per_minute_discovery", int, minimum=1, maximum=60),
            requests_per_minute_pairs=_get(ds, "sources.dexscreener", "requests_per_minute_pairs", int, minimum=1, maximum=300),
        ),
        geckoterminal=GeckoTerminalSettings(
            enabled=_get(gt, "sources.geckoterminal", "enabled", bool),
            use_trending=_get(gt, "sources.geckoterminal", "use_trending", bool),
            use_new_pools=_get(gt, "sources.geckoterminal", "use_new_pools", bool),
            pages=_get(gt, "sources.geckoterminal", "pages", int, minimum=1, maximum=10),
            requests_per_minute=_get(gt, "sources.geckoterminal", "requests_per_minute", int, minimum=1, maximum=30),
        ),
        filters=Filters(
            min_liquidity_usd=_get(flt, "filters", "min_liquidity_usd", float, minimum=0),
            min_volume_1h_usd=_get(flt, "filters", "min_volume_1h_usd", float, minimum=0),
            min_pair_age_minutes=_get(flt, "filters", "min_pair_age_minutes", float, minimum=0),
        ),
        safety=SafetySettings(
            enabled=_get(saf, "safety", "enabled", bool),
            max_checks_per_scan=_get(saf, "safety", "max_checks_per_scan", int, minimum=0, maximum=200),
            recheck_minutes_pass=_get(saf, "safety", "recheck_minutes_pass", int, minimum=1),
            recheck_minutes_fail=_get(saf, "safety", "recheck_minutes_fail", int, minimum=1),
            recheck_minutes_unverified=_get(saf, "safety", "recheck_minutes_unverified", int, minimum=1),
            hide_failed=_get(saf, "safety", "hide_failed", bool),
            strict_mode=_get(saf, "safety", "strict_mode", bool),
            max_top10_holders_pct=_get(saf, "safety", "max_top10_holders_pct", float, minimum=0, maximum=100),
            solana=SolanaSafety(
                require_mint_authority_revoked=_get(saf_sol, "safety.solana", "require_mint_authority_revoked", bool),
                require_freeze_authority_revoked=_get(saf_sol, "safety.solana", "require_freeze_authority_revoked", bool),
                min_lp_locked_pct=_get(saf_sol, "safety.solana", "min_lp_locked_pct", float, minimum=0, maximum=100),
                fail_on_rugcheck_danger=_get(saf_sol, "safety.solana", "fail_on_rugcheck_danger", bool),
                rpc_url=_get(saf_sol, "safety.solana", "rpc_url", str) or "https://api.mainnet-beta.solana.com",
                rugcheck_requests_per_minute=_get(saf_sol, "safety.solana", "rugcheck_requests_per_minute", int, minimum=1, maximum=120),
                rpc_requests_per_minute=_get(saf_sol, "safety.solana", "rpc_requests_per_minute", int, minimum=1, maximum=600),
            ),
            evm=EvmSafety(
                max_buy_tax_pct=_get(saf_evm, "safety.evm", "max_buy_tax_pct", float, minimum=0, maximum=100),
                max_sell_tax_pct=_get(saf_evm, "safety.evm", "max_sell_tax_pct", float, minimum=0, maximum=100),
                fail_if_not_open_source=_get(saf_evm, "safety.evm", "fail_if_not_open_source", bool),
                fail_on_owner_privileges=_get(saf_evm, "safety.evm", "fail_on_owner_privileges", bool),
                use_honeypot_is=_get(saf_evm, "safety.evm", "use_honeypot_is", bool),
                goplus_requests_per_minute=_get(saf_evm, "safety.evm", "goplus_requests_per_minute", int, minimum=1, maximum=30),
                honeypot_is_requests_per_minute=_get(saf_evm, "safety.evm", "honeypot_is_requests_per_minute", int, minimum=1, maximum=120),
            ),
        ),
        momentum=MomentumSettings(
            weights=weights,
            baseline_minutes=_get(mom, "momentum", "baseline_minutes", int, minimum=5),
            volume_acceleration_full_score_ratio=_get(mom, "momentum", "volume_acceleration_full_score_ratio", float, minimum=1.01),
            buy_ratio_full_score=_get(mom, "momentum", "buy_ratio_full_score", float, minimum=0.51, maximum=1.0),
            price_change_1h_full_score_pct=_get(mom, "momentum", "price_change_1h_full_score_pct", float, minimum=0.1),
            giant_candle_pct_5m=_get(mom, "momentum", "giant_candle_pct_5m", float, minimum=0),
            giant_candle_share_of_1h=_get(mom, "momentum", "giant_candle_share_of_1h", float, minimum=0, maximum=1),
            giant_candle_penalty=_get(mom, "momentum", "giant_candle_penalty", float, minimum=0, maximum=1),
        ),
        scoring=ScoringSettings(
            weights=score_weights,
            unverified_safety_multiplier=_get(sco, "scoring", "unverified_safety_multiplier", float, minimum=0, maximum=1),
        ),
        alerts=AlertSettings(
            score_threshold=_get(alr, "alerts", "score_threshold", float, minimum=0, maximum=100),
            cooldown_minutes=_get(alr, "alerts", "cooldown_minutes", int, minimum=0),
            realert_score_jump=_get(alr, "alerts", "realert_score_jump", float, minimum=0, maximum=100),
            send_unverified=_get(alr, "alerts", "send_unverified", bool),
            max_alerts_per_scan=_get(alr, "alerts", "max_alerts_per_scan", int, minimum=1, maximum=50),
            telegram=_get(alr, "alerts", "telegram", bool),
            discord=_get(alr, "alerts", "discord", bool),
        ),
        trade=TradeSettings(
            position_usd=_get(trd, "trade", "position_usd", float, minimum=1),
            take_profit_pct=_get(trd, "trade", "take_profit_pct", float, minimum=0.1, maximum=10000),
            stop_loss_pct=_get(trd, "trade", "stop_loss_pct", float, minimum=0.1, maximum=99),
            round_trip_fee_pct=_get(trd, "trade", "round_trip_fee_pct", float, minimum=0, maximum=50),
            warn_if_costs_exceed_pct_of_tp=_get(trd, "trade", "warn_if_costs_exceed_pct_of_tp", float, minimum=0, maximum=100),
        ),
        display=DisplaySettings(
            top_n=_get(disp, "display", "top_n", int, minimum=1),
            only_passing_filters=_get(disp, "display", "only_passing_filters", bool),
        ),
        storage=StorageSettings(
            database_path=_resolve(_get(sto, "storage", "database_path", str) or "data/scanner.db"),
            keep_snapshots_days=_get(sto, "storage", "keep_snapshots_days", int, minimum=1),
        ),
        network=NetworkSettings(
            timeout_seconds=_get(net, "network", "timeout_seconds", float, minimum=1),
            max_retries=_get(net, "network", "max_retries", int, minimum=0, maximum=10),
        ),
        logging=LoggingSettings(
            log_file=_resolve(_get(log, "logging", "log_file", str) or "logs/scanner.log"),
            max_file_mb=_get(log, "logging", "max_file_mb", float, minimum=0.1),
            backup_count=_get(log, "logging", "backup_count", int, minimum=0),
            level=level,
        ),
        secrets=secrets,
    )
    if not cfg.dexscreener.enabled and not cfg.geckoterminal.enabled:
        raise ConfigError("Both DexScreener and GeckoTerminal are disabled - enable at least one.")
    return cfg
