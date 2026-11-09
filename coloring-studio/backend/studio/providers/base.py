"""Provider-independent image generation interface.

Every adapter implements `ImageProvider`. Adapters declare per-model
capabilities; `map_request()` drops parameters a model does not support so
unsupported options are never sent to a provider.
"""
from __future__ import annotations

import abc
import time
from dataclasses import asdict, dataclass, field
from typing import Any


class ProviderError(Exception):
    """Base provider failure. `retryable` drives the retry policy."""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class ProviderTimeout(ProviderError):
    retryable = True


class ProviderRateLimited(ProviderError):
    retryable = True


class ProviderQuotaExceeded(ProviderError):
    retryable = False


class ProviderAuthError(ProviderError):
    retryable = False


class ProviderNotConfigured(ProviderError):
    retryable = False


class ProviderUnsupported(ProviderError):
    retryable = False


class ProviderContentRejected(ProviderError):
    retryable = False


@dataclass
class ModelCapabilities:
    model_id: str
    display_name: str
    text_to_image: bool = True
    image_edit: bool = False
    mask_edit: bool = False
    reference_images: bool = False
    negative_prompt: bool = False
    seed: bool = False
    aspect_ratios: list[str] = field(default_factory=lambda: ["1:1"])
    sizes: list[tuple[int, int]] = field(default_factory=list)  # native output sizes
    max_side: int = 1024
    output_formats: list[str] = field(default_factory=lambda: ["png"])
    quality_levels: list[str] = field(default_factory=list)
    async_jobs: bool = False
    # Pricing: USD per image; None means unknown. `pricing_note` explains source.
    price_per_image: float | None = None
    price_by_quality: dict[str, float] = field(default_factory=dict)
    pricing_note: str = "unknown"
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sizes"] = [list(s) for s in self.sizes]
        return d


@dataclass
class GenerationRequest:
    project_id: str
    page_id: str | None
    section_id: str | None
    provider_id: str
    model_id: str
    prompt: str
    negative_prompt: str | None = None
    aspect_ratio: str = "3:4"
    width: int = 1536
    height: int = 2048
    style_guide: dict = field(default_factory=dict)
    reference_images: list[bytes] = field(default_factory=list)
    character_references: list[bytes] = field(default_factory=list)
    seed: int | None = None
    mask: bytes | None = None
    init_image: bytes | None = None
    output_format: str = "png"
    quality: str | None = None
    budget_remaining: float | None = None
    timeout_seconds: float = 180.0


@dataclass
class GenerationResult:
    image: bytes
    mime: str
    provider_id: str
    model_id: str
    effective_params: dict
    width: int
    height: int
    provider_job_id: str | None = None
    cost: float | None = None
    cost_is_estimate: bool = True
    duration_seconds: float | None = None
    revised_prompt: str | None = None


@dataclass
class CostEstimate:
    amount: float | None
    is_estimate: bool
    note: str


@dataclass
class ValidationResult:
    ok: bool
    message: str
    models: list[str] = field(default_factory=list)


class ImageProvider(abc.ABC):
    provider_id: str = ""
    display_name: str = ""
    docs_url: str = ""
    requires_api_key: bool = True
    is_mock: bool = False

    def __init__(self, api_key: str | None = None, settings: dict | None = None, transport=None):
        self.api_key = api_key
        self.settings = settings or {}
        self._transport = transport  # httpx transport override for tests

    # --- discovery ----------------------------------------------------------
    @abc.abstractmethod
    def list_models(self) -> list[ModelCapabilities]:
        ...

    def get_capabilities(self, model_id: str) -> ModelCapabilities:
        for m in self.list_models():
            if m.model_id == model_id:
                return m
        raise ProviderUnsupported(f"{self.provider_id}: unknown model '{model_id}'", provider=self.provider_id)

    # --- operations ---------------------------------------------------------
    @abc.abstractmethod
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        ...

    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        raise ProviderUnsupported(f"{self.provider_id} does not support image editing", provider=self.provider_id)

    def get_job_status(self, job_id: str) -> dict:
        return {"job_id": job_id, "status": "unknown"}

    def cancel_job(self, job_id: str) -> bool:
        return False

    def estimate_cost(self, req: GenerationRequest) -> CostEstimate:
        try:
            caps = self.get_capabilities(req.model_id)
        except ProviderUnsupported:
            return CostEstimate(None, True, "unknown model")
        if req.quality and req.quality in caps.price_by_quality:
            return CostEstimate(caps.price_by_quality[req.quality], True, caps.pricing_note)
        return CostEstimate(caps.price_per_image, True, caps.pricing_note)

    @abc.abstractmethod
    def validate_configuration(self) -> ValidationResult:
        ...

    # --- helpers ------------------------------------------------------------
    def require_key(self) -> str:
        if self.requires_api_key and not self.api_key:
            raise ProviderNotConfigured(f"{self.display_name} API key is not configured", provider=self.provider_id)
        return self.api_key or ""

    def client(self, timeout: float = 120.0):
        import httpx
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def map_request(self, req: GenerationRequest) -> dict:
        """Capability-filtered view of the request (what will actually be sent)."""
        caps = self.get_capabilities(req.model_id)
        w, h = choose_size(caps, req.width, req.height)
        params: dict[str, Any] = {"width": w, "height": h, "output_format":
                                  req.output_format if req.output_format in caps.output_formats else caps.output_formats[0]}
        ar = closest_aspect(caps.aspect_ratios, w, h)
        if ar:
            params["aspect_ratio"] = ar
        if caps.negative_prompt and req.negative_prompt:
            params["negative_prompt"] = req.negative_prompt
        if caps.seed and req.seed is not None:
            params["seed"] = req.seed
        if caps.quality_levels:
            params["quality"] = req.quality if req.quality in caps.quality_levels else caps.quality_levels[-1]
        params["uses_reference_images"] = bool(caps.reference_images and (req.reference_images or req.character_references))
        return params


def choose_size(caps: ModelCapabilities, want_w: int, want_h: int) -> tuple[int, int]:
    """Pick the native size closest in aspect to the request (largest area on ties)."""
    target = want_w / want_h
    if caps.sizes:
        return min(caps.sizes, key=lambda s: (abs(s[0] / s[1] - target), -(s[0] * s[1])))
    scale = min(1.0, caps.max_side / max(want_w, want_h))
    w, h = int(want_w * scale) // 16 * 16, int(want_h * scale) // 16 * 16
    return max(w, 256), max(h, 256)


def closest_aspect(options: list[str], w: int, h: int) -> str | None:
    if not options:
        return None
    target = w / h

    def val(a: str) -> float:
        x, y = a.split(":")
        return float(x) / float(y)
    return min(options, key=lambda a: abs(val(a) - target))


def http_error(provider: str, resp) -> ProviderError:
    """Translate an HTTP error response into a typed ProviderError (never echoes headers)."""
    status = resp.status_code
    try:
        body = resp.json()
        detail = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or str(detail)
    except Exception:
        detail = resp.text[:300]
    msg = f"{provider} HTTP {status}: {str(detail)[:300]}"
    low = str(detail).lower()
    if status in (401, 403):
        return ProviderAuthError(msg, status=status, provider=provider)
    if status == 402 or "quota" in low or "insufficient" in low or "billing" in low:
        return ProviderQuotaExceeded(msg, status=status, provider=provider)
    if status == 429:
        return ProviderRateLimited(msg, status=status, provider=provider)
    if status in (408, 504):
        return ProviderTimeout(msg, status=status, provider=provider)
    if status >= 500:
        e = ProviderError(msg, status=status, provider=provider)
        e.retryable = True
        return e
    if "safety" in low or "moderation" in low or "content policy" in low:
        return ProviderContentRejected(msg, status=status, provider=provider)
    return ProviderError(msg, status=status, provider=provider)


def wrap_transport_errors(provider: str):
    """Decorator turning httpx network errors into typed retryable ProviderErrors."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            import httpx
            try:
                return fn(*a, **kw)
            except httpx.TimeoutException as e:
                raise ProviderTimeout(f"{provider}: request timed out", provider=provider) from e
            except httpx.TransportError as e:
                err = ProviderError(f"{provider}: network error ({type(e).__name__})", provider=provider)
                err.retryable = True
                raise err from e
        return inner
    return deco


def poll_until(fn, *, timeout: float, interval: float = 2.0, provider: str = ""):
    """Poll `fn()` until it returns a non-None value or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while True:
        out = fn()
        if out is not None:
            return out
        if time.monotonic() > deadline:
            raise ProviderTimeout(f"{provider}: async job did not finish in {timeout:.0f}s", provider=provider)
        time.sleep(interval)
