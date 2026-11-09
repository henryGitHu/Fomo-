"""Environment-based configuration.

All settings come from environment variables (optionally loaded from a
`.env` file in the backend directory). Nothing secret is hard-coded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides real env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    database_url: str
    secret_key: str | None
    api_token: str | None
    storage_backend: str
    s3_bucket: str | None
    s3_endpoint: str | None
    cors_origins: list[str]
    run_worker: bool
    worker_poll_seconds: float
    job_lease_seconds: int
    research_web_enabled: bool
    text_provider: str
    anthropic_model: str
    openai_text_model: str
    # Provider credentials may also be provided via env (preferred for servers).
    env_credentials: dict[str, str] = field(default_factory=dict)

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


ENV_CREDENTIAL_VARS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "bfl": "BFL_API_KEY",
    "stability": "STABILITY_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "fal": "FAL_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def load_settings() -> Settings:
    _load_dotenv(BACKEND_DIR / ".env")
    data_dir = Path(os.environ.get("STUDIO_DATA_DIR", BACKEND_DIR / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_url = os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'studio.db'}"
    creds = {pid: os.environ[var] for pid, var in ENV_CREDENTIAL_VARS.items() if os.environ.get(var)}
    return Settings(
        data_dir=data_dir,
        database_url=db_url,
        secret_key=os.environ.get("STUDIO_SECRET_KEY") or None,
        api_token=os.environ.get("STUDIO_API_TOKEN") or None,
        storage_backend=os.environ.get("STUDIO_STORAGE", "local"),
        s3_bucket=os.environ.get("S3_BUCKET") or None,
        s3_endpoint=os.environ.get("S3_ENDPOINT_URL") or None,
        cors_origins=[o.strip() for o in os.environ.get(
            "STUDIO_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()],
        run_worker=_bool("STUDIO_RUN_WORKER", True),
        worker_poll_seconds=float(os.environ.get("STUDIO_WORKER_POLL_SECONDS", "0.5")),
        job_lease_seconds=int(os.environ.get("STUDIO_JOB_LEASE_SECONDS", "300")),
        research_web_enabled=_bool("STUDIO_RESEARCH_WEB", True),
        text_provider=os.environ.get("STUDIO_TEXT_PROVIDER", "auto"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        openai_text_model=os.environ.get("OPENAI_TEXT_MODEL", "gpt-5-mini"),
        env_credentials=creds,
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Used by tests after changing environment variables."""
    global _settings
    _settings = None
