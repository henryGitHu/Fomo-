"""Secret handling and request authorization.

Provider API keys entered in the UI are encrypted at rest with Fernet using
STUDIO_SECRET_KEY (or a key file generated in the data dir on first run,
readable only by the owner). Keys are never returned by the API or logged.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        s = get_settings()
        if s.secret_key:
            key = base64.urlsafe_b64encode(hashlib.sha256(s.secret_key.encode()).digest())
        else:
            kf = s.data_dir / ".secret.key"
            if not kf.exists():
                kf.write_bytes(Fernet.generate_key())
                os.chmod(kf, 0o600)
            key = kf.read_bytes().strip()
        _fernet = Fernet(key)
    return _fernet


def reset_crypto() -> None:
    global _fernet
    _fernet = None


def encrypt_secret(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * len(value)


_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"r8_[A-Za-z0-9]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
]
_HEADER_PATTERN = re.compile(r"((?:Bearer|Key|Token)\s+)[A-Za-z0-9_\-\.:]{8,}", re.I)


def redact(text: str | None, secrets: list[str] | None = None) -> str:
    """Remove anything that looks like a credential from text bound for logs/UI."""
    if not text:
        return text or ""
    out = text
    for s in secrets or []:
        if s:
            out = out.replace(s, "[REDACTED]")
    for pat in _TOKEN_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return _HEADER_PATTERN.sub(lambda m: m.group(1) + "[REDACTED]", out)
