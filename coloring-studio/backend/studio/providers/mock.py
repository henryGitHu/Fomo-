"""Deterministic offline providers used for development and automated tests.

They draw procedural black-and-white line art seeded by the prompt, so the
same request always yields the same image and different prompts yield
different images. Output is clearly labelled as simulated in the UI.

Failure injection (settings["fail_mode"]) lets tests exercise retries and
recovery: "timeout_once", "timeout_always", "quota", "rate_limit_once".
"""
from __future__ import annotations

import hashlib
import io
import math
import random
import threading
import time

from PIL import Image, ImageDraw

from .base import (CostEstimate, GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities,
                   ProviderQuotaExceeded, ProviderRateLimited, ProviderTimeout, ValidationResult)

_fail_lock = threading.Lock()
_fail_counters: dict[str, int] = {}


def reset_failures() -> None:
    with _fail_lock:
        _fail_counters.clear()


def _rng(*parts) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def draw_line_art(prompt: str, width: int, height: int, *, line_width: int, seed=None, variant: str = "a",
                  detail: str = "medium") -> Image.Image:
    rng = _rng(prompt, seed, variant)
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    lw = max(2, line_width)
    pad = int(min(width, height) * 0.06)

    # ground line + horizon details
    gy = int(height * rng.uniform(0.78, 0.86))
    d.line([(pad, gy), (width - pad, gy)], fill=0, width=lw)

    # main subject: composite "machine/creature" built from primitives
    cx = width // 2 + rng.randint(-width // 10, width // 10)
    body_w = int(width * rng.uniform(0.35, 0.5))
    body_h = int(height * rng.uniform(0.35, 0.48))
    top = gy - body_h
    shape = rng.choice(["tank", "tower", "round", "house"]) if variant == "a" else rng.choice(["round", "tower", "tank"])
    if shape == "tank":
        d.rounded_rectangle([cx - body_w // 2, top, cx + body_w // 2, gy], radius=body_w // 6, outline=0, width=lw)
        for i in range(rng.randint(2, 4)):
            r = body_w // rng.randint(8, 12)
            gx = cx - body_w // 3 + i * (body_w // 4)
            d.ellipse([gx - r, top + body_h // 4 - r, gx + r, top + body_h // 4 + r], outline=0, width=lw)
    elif shape == "tower":
        tw = body_w // 2
        d.rectangle([cx - tw // 2, top - body_h // 4, cx + tw // 2, gy], outline=0, width=lw)
        for k in range(1, rng.randint(4, 7)):
            yy = top - body_h // 4 + k * (body_h // 6)
            d.line([(cx - tw // 2, yy), (cx + tw // 2, yy)], fill=0, width=max(2, lw // 2 + 1))
        d.ellipse([cx - tw // 2, top - body_h // 4 - tw // 4, cx + tw // 2, top - body_h // 4 + tw // 4], outline=0, width=lw)
    elif shape == "round":
        r = min(body_w, body_h) // 2
        d.ellipse([cx - r, gy - 2 * r, cx + r, gy], outline=0, width=lw)
        d.ellipse([cx - r // 2, gy - int(1.4 * r), cx + r // 2, gy - int(0.6 * r)], outline=0, width=lw)
    else:
        d.rectangle([cx - body_w // 2, top + body_h // 3, cx + body_w // 2, gy], outline=0, width=lw)
        d.polygon([(cx - body_w // 2 - lw, top + body_h // 3), (cx, top), (cx + body_w // 2 + lw, top + body_h // 3)],
                  outline=0, width=lw)
        d.rectangle([cx - body_w // 10, gy - body_h // 3, cx + body_w // 10, gy], outline=0, width=lw)

    # pipes / arms
    for _ in range(rng.randint(1, 3)):
        side = rng.choice([-1, 1])
        y = rng.randint(top + body_h // 5, gy - body_h // 5)
        x0 = cx + side * body_w // 2
        x1 = x0 + side * rng.randint(width // 12, width // 5)
        x1 = max(pad, min(width - pad, x1))
        d.line([(x0, y), (x1, y)], fill=0, width=lw)
        d.line([(x1, y), (x1, gy)], fill=0, width=lw)

    # secondary elements (count depends on detail density)
    n_extra = {"low": 3, "medium": 6, "high": 10}.get(detail, 6)
    for _ in range(n_extra):
        kind = rng.choice(["circle", "star", "cloud", "square"])
        ex = rng.randint(pad * 2, width - pad * 2)
        ey = rng.randint(pad * 2, max(pad * 2 + 1, top - pad)) if top > pad * 4 else rng.randint(gy + pad // 2, height - pad)
        s = rng.randint(min(width, height) // 30, min(width, height) // 14)
        if kind == "circle":
            d.ellipse([ex - s, ey - s, ex + s, ey + s], outline=0, width=lw)
        elif kind == "square":
            d.rectangle([ex - s, ey - s, ex + s, ey + s], outline=0, width=lw)
        elif kind == "star":
            pts = []
            for k in range(10):
                ang = math.pi / 2 + k * math.pi / 5
                rr = s if k % 2 == 0 else s * 0.45
                pts.append((ex + rr * math.cos(ang), ey - rr * math.sin(ang)))
            d.polygon(pts, outline=0, width=max(2, lw - 1))
        else:
            for k in range(3):
                d.ellipse([ex - s + k * s // 1.5, ey - s // 2, ex + k * s // 1.5, ey + s // 2], outline=0, width=lw)
    return img


class MockProvider(ImageProvider):
    provider_id = "mock"
    display_name = "Mock Studio (offline, simulated)"
    requires_api_key = False
    is_mock = True
    variant = "a"
    base_line = 7

    def list_models(self) -> list[ModelCapabilities]:
        price = self.settings.get("simulated_price_per_image", 0.0)
        note = "simulated price (mock provider; no real charge)"
        return [
            ModelCapabilities("mock-lineart-v1", "Mock Line Art v1", image_edit=True, mask_edit=False,
                              reference_images=True, negative_prompt=True, seed=True,
                              aspect_ratios=["1:1", "3:4", "4:3"], max_side=2400,
                              price_per_image=price, pricing_note=note),
            ModelCapabilities("mock-lineart-bold", "Mock Bold Outlines", image_edit=True, seed=True,
                              aspect_ratios=["1:1", "3:4"], max_side=2048,
                              price_per_image=price, pricing_note=note),
        ]

    def _maybe_fail(self) -> None:
        mode = self.settings.get("fail_mode")
        if not mode:
            return
        key = f"{self.provider_id}:{mode}"
        with _fail_lock:
            n = _fail_counters.get(key, 0)
            _fail_counters[key] = n + 1
        if mode == "timeout_always" or (mode == "timeout_once" and n == 0):
            raise ProviderTimeout(f"{self.provider_id}: simulated timeout", provider=self.provider_id)
        if mode == "rate_limit_once" and n == 0:
            raise ProviderRateLimited(f"{self.provider_id}: simulated 429", provider=self.provider_id)
        if mode == "quota":
            raise ProviderQuotaExceeded(f"{self.provider_id}: simulated quota exceeded", provider=self.provider_id)

    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        self._maybe_fail()
        delay = float(self.settings.get("simulated_latency", 0.0))
        if delay:
            time.sleep(delay)
        params = self.map_request(req)
        w, h = params["width"], params["height"]
        lw = self.base_line + (4 if req.model_id.endswith("bold") else 0)
        lw += int(req.style_guide.get("line_weight_px_adjust", 0))
        detail = req.style_guide.get("detail_density", "medium")
        img = draw_line_art(req.prompt, w, h, line_width=lw, seed=req.seed, variant=self.variant, detail=detail)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        est = self.estimate_cost(req)
        return GenerationResult(image=buf.getvalue(), mime="image/png", provider_id=self.provider_id,
                                model_id=req.model_id, effective_params=params, width=w, height=h,
                                provider_job_id=f"mock-{hashlib.sha1(req.prompt.encode()).hexdigest()[:10]}",
                                cost=est.amount, cost_is_estimate=est.amount is None,
                                duration_seconds=time.monotonic() - t0)

    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        """Simulated edit: keeps the source image and overlays prompt-derived elements."""
        if not req.init_image:
            return self.generate_image(req)
        self._maybe_fail()
        base = Image.open(io.BytesIO(req.init_image)).convert("L")
        overlay = draw_line_art(req.prompt, base.width, base.height, line_width=self.base_line, seed=req.seed,
                                variant=self.variant, detail="low")
        from PIL import ImageChops
        out = ImageChops.darker(base, overlay)
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return GenerationResult(image=buf.getvalue(), mime="image/png", provider_id=self.provider_id,
                                model_id=req.model_id, effective_params={"edit": True, "width": base.width,
                                                                         "height": base.height},
                                width=base.width, height=base.height, cost=self.estimate_cost(req).amount,
                                cost_is_estimate=False, duration_seconds=0.0)

    def estimate_cost(self, req: GenerationRequest) -> CostEstimate:
        price = self.settings.get("simulated_price_per_image", 0.0)
        if price is None:  # lets tests exercise "unknown price" handling
            return CostEstimate(None, True, "simulated unknown price")
        return CostEstimate(float(price), False, "simulated")

    def validate_configuration(self) -> ValidationResult:
        return ValidationResult(True, "Offline mock provider is always available (simulated output).",
                                [m.model_id for m in self.list_models()])


class MockAltProvider(MockProvider):
    """A second, independent-looking offline provider (different drawing variant)."""
    provider_id = "mock_alt"
    display_name = "Mock Atelier (offline, simulated)"
    variant = "b"
    base_line = 9

    def list_models(self) -> list[ModelCapabilities]:
        price = self.settings.get("simulated_price_per_image", 0.0)
        return [ModelCapabilities("atelier-sketch-1", "Atelier Sketch 1", image_edit=True, seed=True,
                                  aspect_ratios=["1:1", "3:4"], max_side=2048, price_per_image=price,
                                  pricing_note="simulated price (mock provider; no real charge)")]
