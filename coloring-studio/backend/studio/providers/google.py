"""Google Gemini API adapter (Gemini native image models and Imagen).

Gemini image: POST /v1beta/models/{model}:generateContent (responseModalities IMAGE)
Imagen:       POST /v1beta/models/{model}:predict
Docs: https://ai.google.dev/gemini-api/docs/image-generation , https://ai.google.dev/gemini-api/docs/imagen
"""
from __future__ import annotations

import base64
import io
import time

from .base import (GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities, ProviderContentRejected,
                   ProviderError, ValidationResult, http_error, wrap_transport_errors)

API = "https://generativelanguage.googleapis.com/v1beta"
_AR = ["1:1", "3:4", "4:3", "9:16", "16:9"]


class GoogleImageProvider(ImageProvider):
    provider_id = "google"
    display_name = "Google Gemini / Imagen"
    docs_url = "https://ai.google.dev/gemini-api/docs/image-generation"

    def list_models(self) -> list[ModelCapabilities]:
        note = "approximate list price; verify on ai.google.dev/pricing"
        return [
            ModelCapabilities("gemini-2.5-flash-image", "Gemini 2.5 Flash Image", image_edit=True,
                              reference_images=True, aspect_ratios=_AR + ["2:3", "3:2"], max_side=1344,
                              price_per_image=0.039, pricing_note=note,
                              notes="Supports conversational edits and multi-image references"),
            ModelCapabilities("imagen-4.0-generate-001", "Imagen 4", aspect_ratios=_AR, max_side=1408,
                              negative_prompt=False, price_per_image=0.04, pricing_note=note),
            ModelCapabilities("imagen-4.0-ultra-generate-001", "Imagen 4 Ultra", aspect_ratios=_AR, max_side=2048,
                              price_per_image=0.06, pricing_note=note),
        ]

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.require_key(), "Content-Type": "application/json"}

    @wrap_transport_errors("google")
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        if req.model_id.startswith("imagen"):
            return self._imagen(req)
        return self._gemini(req, edit=False)

    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        if req.model_id.startswith("imagen"):
            return super().edit_image(req)
        return self._gemini(req, edit=True)

    @wrap_transport_errors("google")
    def _gemini(self, req: GenerationRequest, edit: bool) -> GenerationResult:
        p = self.map_request(req)
        parts: list[dict] = [{"text": req.prompt}]
        images = ([req.init_image] if (edit and req.init_image) else []) + req.reference_images[:3]
        for img in images:
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"],
                                     "imageConfig": {"aspectRatio": p.get("aspect_ratio", "3:4")}}}
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{API}/models/{req.model_id}:generateContent", json=body, headers=self._headers())
        if r.status_code >= 400:
            raise http_error("google", r)
        data = r.json()
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                blob = part.get("inlineData") or part.get("inline_data")
                if blob and blob.get("data"):
                    return self._wrap(base64.b64decode(blob["data"]), req, {"aspect_ratio": p.get("aspect_ratio"),
                                                                            "edit": edit}, t0)
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        if reason:
            raise ProviderContentRejected(f"google: prompt blocked ({reason})", provider="google")
        raise ProviderError("google: response contained no image", provider="google")

    @wrap_transport_errors("google")
    def _imagen(self, req: GenerationRequest) -> GenerationResult:
        p = self.map_request(req)
        body = {"instances": [{"prompt": req.prompt}],
                "parameters": {"sampleCount": 1, "aspectRatio": p.get("aspect_ratio", "3:4")}}
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{API}/models/{req.model_id}:predict", json=body, headers=self._headers())
        if r.status_code >= 400:
            raise http_error("google", r)
        preds = r.json().get("predictions") or []
        if not preds or "bytesBase64Encoded" not in preds[0]:
            raise ProviderContentRejected("google: no image returned (possibly filtered)", provider="google")
        return self._wrap(base64.b64decode(preds[0]["bytesBase64Encoded"]), req, body["parameters"], t0)

    def _wrap(self, img: bytes, req: GenerationRequest, params: dict, t0: float) -> GenerationResult:
        from PIL import Image
        with Image.open(io.BytesIO(img)) as im:
            w, h = im.size
            mime = Image.MIME.get(im.format, "image/png")
        return GenerationResult(image=img, mime=mime, provider_id=self.provider_id, model_id=req.model_id,
                                effective_params=params, width=w, height=h, cost=self.estimate_cost(req).amount,
                                cost_is_estimate=True, duration_seconds=time.monotonic() - t0)

    @wrap_transport_errors("google")
    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API key configured")
        with self.client(20) as c:
            r = c.get(f"{API}/models", headers=self._headers(), params={"pageSize": 200})
        if r.status_code >= 400:
            return ValidationResult(False, str(http_error("google", r)))
        names = {m.get("name", "").split("/")[-1] for m in r.json().get("models", [])}
        ours = [m.model_id for m in self.list_models() if m.model_id in names]
        return ValidationResult(True, f"Authenticated; {len(ours)} supported image model(s) visible", ours)
