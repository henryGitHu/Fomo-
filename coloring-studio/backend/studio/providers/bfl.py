"""Black Forest Labs FLUX adapter (official API, async submit + poll).

POST https://api.bfl.ai/v1/{model} -> {id, polling_url}; GET polling_url until status == "Ready".
Docs: https://docs.bfl.ai
"""
from __future__ import annotations

import base64
import io
import time

from .base import (GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities, ProviderContentRejected,
                   ProviderError, ValidationResult, http_error, poll_until, wrap_transport_errors)

API = "https://api.bfl.ai/v1"


class BFLProvider(ImageProvider):
    provider_id = "bfl"
    display_name = "Black Forest Labs (FLUX)"
    docs_url = "https://docs.bfl.ai"

    def list_models(self) -> list[ModelCapabilities]:
        note = "approximate list price; verify on bfl.ai/pricing"
        return [
            ModelCapabilities("flux-pro-1.1", "FLUX1.1 [pro]", seed=True, max_side=1440, async_jobs=True,
                              aspect_ratios=["1:1", "3:4", "4:3", "2:3"], price_per_image=0.04, pricing_note=note),
            ModelCapabilities("flux-pro-1.1-ultra", "FLUX1.1 [pro] Ultra", seed=True, max_side=2752, async_jobs=True,
                              aspect_ratios=["1:1", "3:4", "4:3", "2:3", "9:16"], price_per_image=0.06,
                              pricing_note=note),
            ModelCapabilities("flux-kontext-pro", "FLUX.1 Kontext [pro]", image_edit=True, reference_images=True,
                              seed=True, max_side=1440, async_jobs=True, aspect_ratios=["1:1", "3:4", "4:3"],
                              price_per_image=0.04, pricing_note=note),
        ]

    def _headers(self) -> dict:
        return {"x-key": self.require_key(), "accept": "application/json"}

    def _body(self, req: GenerationRequest, p: dict, init: bytes | None) -> dict:
        body: dict = {"prompt": req.prompt, "output_format": "png"}
        if req.model_id == "flux-pro-1.1":
            body["width"] = p["width"] // 32 * 32
            body["height"] = p["height"] // 32 * 32
        else:
            body["aspect_ratio"] = p.get("aspect_ratio", "3:4")
        if "seed" in p:
            body["seed"] = p["seed"]
        if init is not None:
            body["input_image"] = base64.b64encode(init).decode()
        return body

    @wrap_transport_errors("bfl")
    def _run(self, req: GenerationRequest, init: bytes | None) -> GenerationResult:
        p = self.map_request(req)
        body = self._body(req, p, init)
        t0 = time.monotonic()
        with self.client(60) as c:
            r = c.post(f"{API}/{req.model_id}", json=body, headers=self._headers())
            if r.status_code >= 400:
                raise http_error("bfl", r)
            sub = r.json()
            job_id, poll_url = sub.get("id"), sub.get("polling_url") or f"{API}/get_result?id={sub.get('id')}"

            def check():
                pr = c.get(poll_url, headers=self._headers())
                if pr.status_code >= 400:
                    raise http_error("bfl", pr)
                js = pr.json()
                st = js.get("status")
                if st == "Ready":
                    return js
                if st in ("Error", "Failed"):
                    raise ProviderError(f"bfl: job failed ({js.get('details') or st})", provider="bfl")
                if st in ("Content Moderated", "Request Moderated"):
                    raise ProviderContentRejected(f"bfl: {st}", provider="bfl")
                return None
            res = poll_until(check, timeout=req.timeout_seconds, interval=float(self.settings.get("poll_interval", 1.5)),
                             provider="bfl")
            url = res["result"]["sample"]
            img_r = c.get(url)
            if img_r.status_code >= 400:
                raise http_error("bfl", img_r)
            img = img_r.content
        from PIL import Image
        with Image.open(io.BytesIO(img)) as im:
            w, h = im.size
        sent = {k: v for k, v in body.items() if k not in ("prompt", "input_image")}
        return GenerationResult(image=img, mime="image/png", provider_id=self.provider_id, model_id=req.model_id,
                                effective_params=sent, width=w, height=h, provider_job_id=job_id,
                                cost=self.estimate_cost(req).amount, cost_is_estimate=True,
                                duration_seconds=time.monotonic() - t0)

    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        return self._run(req, None)

    def edit_image(self, req: GenerationRequest) -> GenerationResult:
        if not self.get_capabilities(req.model_id).image_edit:
            return super().edit_image(req)
        return self._run(req, req.init_image)

    def get_job_status(self, job_id: str) -> dict:
        with self.client(20) as c:
            r = c.get(f"{API}/get_result", params={"id": job_id}, headers=self._headers())
        return r.json() if r.status_code < 400 else {"job_id": job_id, "status": f"HTTP {r.status_code}"}

    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API key configured")
        # BFL has no free authenticated listing endpoint; query a non-existent result id:
        # 401/403 => bad key, anything else => key accepted.
        try:
            with self.client(20) as c:
                r = c.get(f"{API}/get_result", params={"id": "connection-test"}, headers=self._headers())
        except Exception as e:  # network
            return ValidationResult(False, f"bfl: network error ({type(e).__name__})")
        if r.status_code in (401, 403):
            return ValidationResult(False, "bfl: API key rejected")
        return ValidationResult(True, "Key accepted (no generation performed)", [m.model_id for m in self.list_models()])
