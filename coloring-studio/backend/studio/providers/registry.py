"""Provider registry: resolves configured, credentialed provider instances."""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ProviderConfig
from ..security import decrypt_secret
from .base import ImageProvider, ProviderNotConfigured
from .bfl import BFLProvider
from .google import GoogleImageProvider
from .hosted import FalProvider, ReplicateProvider
from .mock import MockAltProvider, MockProvider
from .openai_images import OpenAIImageProvider
from .stability import StabilityProvider

PROVIDER_CLASSES: dict[str, type[ImageProvider]] = {
    cls.provider_id: cls for cls in (
        MockProvider, MockAltProvider, OpenAIImageProvider, GoogleImageProvider, BFLProvider,
        StabilityProvider, ReplicateProvider, FalProvider)
}

# Test hook: provider_id -> factory(api_key, settings) used instead of the class.
_overrides: dict[str, Callable[[str | None, dict], ImageProvider]] = {}


def set_override(provider_id: str, factory: Callable[[str | None, dict], ImageProvider] | None) -> None:
    if factory is None:
        _overrides.pop(provider_id, None)
    else:
        _overrides[provider_id] = factory


def get_config(db: Session, provider_id: str) -> ProviderConfig:
    cfg = db.get(ProviderConfig, provider_id)
    if cfg is None:
        if provider_id not in PROVIDER_CLASSES:
            raise ProviderNotConfigured(f"unknown provider '{provider_id}'", provider=provider_id)
        cls = PROVIDER_CLASSES[provider_id]
        # Mock providers are enabled by default; real ones once a key exists.
        cfg = ProviderConfig(provider_id=provider_id, enabled=True,
                             priority=10 if cls.is_mock else 50)
        db.add(cfg)
        db.flush()
    return cfg


def resolve_api_key(cfg: ProviderConfig) -> tuple[str | None, str]:
    """Returns (key, source) where source is 'stored', 'env' or 'none'."""
    key = decrypt_secret(cfg.encrypted_api_key)
    if key:
        return key, "stored"
    env = get_settings().env_credentials.get(cfg.provider_id)
    if env:
        return env, "env"
    return None, "none"


def get_provider(db: Session, provider_id: str, *, require_enabled: bool = True) -> ImageProvider:
    cfg = get_config(db, provider_id)
    if require_enabled and not cfg.enabled:
        raise ProviderNotConfigured(f"provider '{provider_id}' is disabled", provider=provider_id)
    key, _ = resolve_api_key(cfg)
    settings = dict(cfg.settings or {})
    if provider_id in _overrides:
        return _overrides[provider_id](key, settings)
    return PROVIDER_CLASSES[provider_id](api_key=key, settings=settings)


def is_usable(db: Session, provider_id: str) -> bool:
    cfg = get_config(db, provider_id)
    if not cfg.enabled:
        return False
    cls = PROVIDER_CLASSES[provider_id]
    if not cls.requires_api_key or provider_id in _overrides:
        return True
    key, _ = resolve_api_key(cfg)
    return bool(key)


def all_provider_ids() -> list[str]:
    return list(PROVIDER_CLASSES)
