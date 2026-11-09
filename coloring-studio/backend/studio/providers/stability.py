"""Stability AI adapter (Stable Image v2beta REST API).

Generate: POST /v2beta/stable-image/generate/{core|ultra|sd3}  (multipart, Accept: image/*)
Edit:     POST /v2beta/stable-image/edit/inpaint
Upscale:  POST /v2beta/stable-image/upscale/conservative
Docs: https://platform.stability.ai/docs/api-reference
"""
from __future__ import annotations

import io
import time

from .base import (GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities, ValidationResult,
                   http_error, wrap_transport_errors)

API = "https://api.stability.ai"
_AR = ["1:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9", "21:9", "9:21"]
_ENDPOINT = {"stable-image-core": "core", "stable-image-ultra": "ultra", "sd3.5-large": "sd3"}


class StabilityProvider(ImageProvider):
    provider_id = "stability"
    display_name = "Stability AI"
    docs_url = "https://platform.stability.ai/docs/api-reference"

    def list_models(self) -> list[ModelCapabilities]:
        note = "credit-based; approximate USD at $0.01/credit; verify on platform.stability.ai/pricing"
        common = dict(negative_prompt=True, seed=True, aspect_ratios=_AR, output_formats=["png", "jpeg", "webp"],
                      mask_edit=True, image_edit=True)
        return [
            ModelCapabilities("stable-image-core", "Stable Image Core", max_side=1536, price_per_image=0.03,
                              pricing_note=note, **common),
            ModelCapabilities("stable-image-ultra", "Stable Image Ultra", max_side=1536, price_per_image=0.08,
                              pricing_note=note, **common),
            ModelCapabilities("sd3.5-large", "Stable Diffusion 3.5 Large", max_side=1536, price_per_image=0.065,
                              pricing_note=note, **common),
        ]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.require_key()}", "Accept": "image/*"}

    @wrap_transport_errors("stability")
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        p = self.map_request(req)
        fields = {"prompt": req.prompt, "aspect_ratio": p.get("aspect_ratio", "2:3"), "output_format": p["output_format"]}
        if "negative_prompt" in p:
            fields["negative_prompt"] = p["negative_prompt"]
        if "seed" in p:
            fields["seed"] = str(p["seed"])
        if req.model_id == "sd3.5-large":
            fields["model"] = "sd3.5-large"
        endpoint = _ENDPOINT[req.model_id]
        return self._post(f"{API}/v2beta/stable-image/generate/{endpoint}", fields, None, req)

    @wrap_transport_errors("stability")
    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        if not req.init_image:
            return self.generate_image(req)
        fields = {"prompt": req.prompt, "output_format": "png"}
        files = {"image": ("image.png", req.init_image, "image/png")}
        if req.mask:
            files["mask"] = ("mask.png", req.mask, "image/png")
        return self._post(f"{API}/v2beta/stable-image/edit/inpaint", fields, files, req)

    def _post(self, url: str, fields: dict, files: dict | None, req: GenerationRequest) -> GenerationResult:
        t0 = time.monotonic()
        multipart = files or {"none": ("", b"")}  # API requires multipart/form-data even without files
        with self.client(req.timeout_seconds) as c:
            r = c.post(url, data=fields, files=multipart, headers=self._headers())
        if r.status_code >= 400:
            raise http_error("stability", r)
        from PIL import Image
        with Image.open(io.BytesIO(r.content)) as im:
            w, h = im.size
        sent = {k: v for k, v in fields.items() if k != "prompt"}
        return GenerationResult(image=r.content, mime=r.headers.get("content-type", "image/png"),
                                provider_id=self.provider_id, model_id=req.model_id, effective_params=sent,
                                width=w, height=h, provider_job_id=r.headers.get("x-request-id"),
                                cost=self.estimate_cost(req).amount, cost_is_estimate=True,
                                duration_seconds=time.monotonic() - t0)

    @wrap_transport_errors("stability")
    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API key configured")
        with self.client(20) as c:
            r = c.get(f"{API}/v1/user/balance", headers={"Authorization": f"Bearer {self.api_key}"})
        if r.status_code >= 400:
            return ValidationResult(False, str(http_error("stability", r)))
        credits = r.json().get("credits")
        return ValidationResult(True, f"Authenticated; balance {credits} credits", [m.model_id for m in self.list_models()])
