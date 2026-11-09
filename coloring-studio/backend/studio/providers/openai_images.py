"""OpenAI Images API adapter (GPT Image models).

Endpoints: POST /v1/images/generations, POST /v1/images/edits, GET /v1/models.
Docs: https://platform.openai.com/docs/api-reference/images
Pricing figures are approximate list prices and must be re-verified on the
official pricing page before relying on them.
"""
from __future__ import annotations

import base64
import time

from .base import (GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities, ValidationResult,
                   http_error, wrap_transport_errors)

API = "https://api.openai.com/v1"
_PORTRAIT_SIZES = [(1024, 1024), (1024, 1536), (1536, 1024)]


class OpenAIImageProvider(ImageProvider):
    provider_id = "openai"
    display_name = "OpenAI"
    docs_url = "https://platform.openai.com/docs/guides/image-generation"

    def list_models(self) -> list[ModelCapabilities]:
        note = "approximate list price for 1024x1536; verify on openai.com/api/pricing"
        return [
            ModelCapabilities("gpt-image-1", "GPT Image 1", image_edit=True, mask_edit=True, reference_images=True,
                              sizes=_PORTRAIT_SIZES, aspect_ratios=["1:1", "2:3", "3:2"], max_side=1536,
                              output_formats=["png", "jpeg", "webp"], quality_levels=["low", "medium", "high"],
                              price_by_quality={"low": 0.016, "medium": 0.063, "high": 0.25},
                              price_per_image=0.25, pricing_note=note),
            ModelCapabilities("gpt-image-1-mini", "GPT Image 1 Mini", image_edit=True, mask_edit=True,
                              reference_images=True, sizes=_PORTRAIT_SIZES, aspect_ratios=["1:1", "2:3", "3:2"],
                              max_side=1536, output_formats=["png", "jpeg", "webp"],
                              quality_levels=["low", "medium", "high"], price_per_image=None,
                              pricing_note="pricing not verified in this build; shown as unknown"),
        ]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.require_key()}"}

    @wrap_transport_errors("openai")
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        p = self.map_request(req)
        body = {"model": req.model_id, "prompt": req.prompt, "n": 1, "size": f"{p['width']}x{p['height']}",
                "quality": p.get("quality", "high"), "output_format": p["output_format"]}
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{API}/images/generations", json=body, headers=self._headers())
        if r.status_code >= 400:
            raise http_error("openai", r)
        return self._result(r.json(), req, body, t0)

    @wrap_transport_errors("openai")
    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        if not req.init_image:
            return self.generate_image(req)
        p = self.map_request(req)
        data = {"model": req.model_id, "prompt": req.prompt, "n": "1", "size": f"{p['width']}x{p['height']}",
                "quality": p.get("quality", "high")}
        files = [("image[]", ("source.png", req.init_image, "image/png"))]
        for i, ref in enumerate(req.reference_images[:3]):
            files.append(("image[]", (f"ref{i}.png", ref, "image/png")))
        if req.mask:
            files.append(("mask", ("mask.png", req.mask, "image/png")))
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{API}/images/edits", data=data, files=files, headers=self._headers())
        if r.status_code >= 400:
            raise http_error("openai", r)
        return self._result(r.json(), req, data, t0)

    def _result(self, payload: dict, req: GenerationRequest, sent: dict, t0: float) -> GenerationResult:
        item = payload["data"][0]
        img = base64.b64decode(item["b64_json"])
        w, h = (int(x) for x in sent["size"].split("x"))
        est = self.estimate_cost(GenerationRequest(**{**req.__dict__, "quality": sent.get("quality")}))
        return GenerationResult(image=img, mime="image/png", provider_id=self.provider_id, model_id=req.model_id,
                                effective_params={k: v for k, v in sent.items() if k != "prompt"}, width=w, height=h,
                                provider_job_id=payload.get("created") and str(payload.get("created")),
                                cost=est.amount, cost_is_estimate=True, duration_seconds=time.monotonic() - t0,
                                revised_prompt=item.get("revised_prompt"))

    @wrap_transport_errors("openai")
    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API key configured")
        with self.client(20) as c:
            r = c.get(f"{API}/models", headers=self._headers())
        if r.status_code >= 400:
            return ValidationResult(False, str(http_error("openai", r)))
        ids = {m["id"] for m in r.json().get("data", [])}
        ours = [m.model_id for m in self.list_models() if m.model_id in ids]
        return ValidationResult(True, f"Authenticated; {len(ours)} supported image model(s) visible", ours)
