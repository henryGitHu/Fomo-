"""Hosted-model platforms: Replicate and fal.ai (async submit + poll)."""
from __future__ import annotations

import io
import time

from .base import (GenerationRequest, GenerationResult, ImageProvider, ModelCapabilities, ProviderError,
                   ValidationResult, http_error, poll_until, wrap_transport_errors)


def _dims(img: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(img)) as im:
        return im.size


class ReplicateProvider(ImageProvider):
    """Replicate predictions API. Docs: https://replicate.com/docs/reference/http"""
    provider_id = "replicate"
    display_name = "Replicate"
    docs_url = "https://replicate.com/docs/reference/http"
    API = "https://api.replicate.com/v1"

    def list_models(self) -> list[ModelCapabilities]:
        note = "approximate per-image price of the hosted model; verify on its Replicate page"
        ar = ["1:1", "2:3", "3:4", "4:3", "3:2"]
        return [
            ModelCapabilities("black-forest-labs/flux-schnell", "FLUX schnell (Replicate)", seed=True, async_jobs=True,
                              aspect_ratios=ar, max_side=1440, price_per_image=0.003, pricing_note=note),
            ModelCapabilities("black-forest-labs/flux-1.1-pro", "FLUX1.1 pro (Replicate)", seed=True, async_jobs=True,
                              aspect_ratios=ar, max_side=1440, price_per_image=0.04, pricing_note=note),
        ] + [ModelCapabilities(m, m, async_jobs=True, aspect_ratios=ar, pricing_note="custom model; price unknown")
             for m in self.settings.get("extra_models", [])]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.require_key()}", "Content-Type": "application/json",
                "Prefer": "wait=60"}

    @wrap_transport_errors("replicate")
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        p = self.map_request(req)
        inp = {"prompt": req.prompt, "aspect_ratio": p.get("aspect_ratio", "3:4"), "output_format": "png"}
        if "seed" in p:
            inp["seed"] = p["seed"]
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{self.API}/models/{req.model_id}/predictions", json={"input": inp}, headers=self._headers())
            if r.status_code >= 400:
                raise http_error("replicate", r)
            pred = r.json()

            state = {"pred": pred, "fresh": True}

            def check():
                if not state["fresh"]:
                    pr = c.get(state["pred"]["urls"]["get"], headers=self._headers())
                    if pr.status_code >= 400:
                        raise http_error("replicate", pr)
                    state["pred"] = pr.json()
                state["fresh"] = False
                cur = state["pred"]
                st = cur.get("status")
                if st == "succeeded":
                    return cur
                if st in ("failed", "canceled"):
                    raise ProviderError(f"replicate: prediction {st}: {cur.get('error')}", provider="replicate")
                return None
            done = poll_until(check, timeout=req.timeout_seconds,
                              interval=float(self.settings.get("poll_interval", 1.5)), provider="replicate")
            out = done["output"]
            url = out[0] if isinstance(out, list) else out
            img = c.get(url).content
        w, h = _dims(img)
        return GenerationResult(image=img, mime="image/png", provider_id=self.provider_id, model_id=req.model_id,
                                effective_params={k: v for k, v in inp.items() if k != "prompt"}, width=w, height=h,
                                provider_job_id=done.get("id"), cost=self.estimate_cost(req).amount,
                                cost_is_estimate=True, duration_seconds=time.monotonic() - t0)

    def cancel_job(self, job_id: str) -> bool:
        with self.client(20) as c:
            r = c.post(f"{self.API}/predictions/{job_id}/cancel", headers=self._headers())
        return r.status_code < 400

    @wrap_transport_errors("replicate")
    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API token configured")
        with self.client(20) as c:
            r = c.get(f"{self.API}/account", headers=self._headers())
        if r.status_code >= 400:
            return ValidationResult(False, str(http_error("replicate", r)))
        return ValidationResult(True, f"Authenticated as {r.json().get('username')}", [m.model_id for m in self.list_models()])


class FalProvider(ImageProvider):
    """fal.ai queue API. Docs: https://docs.fal.ai/model-endpoints/queue"""
    provider_id = "fal"
    display_name = "fal.ai"
    docs_url = "https://docs.fal.ai/model-endpoints/queue"
    QUEUE = "https://queue.fal.run"

    def list_models(self) -> list[ModelCapabilities]:
        note = "fal bills per megapixel; approximate for ~1MP; verify on fal.ai/pricing"
        return [
            ModelCapabilities("fal-ai/flux/schnell", "FLUX schnell (fal)", seed=True, async_jobs=True, max_side=2048,
                              aspect_ratios=["1:1", "3:4", "4:3"], price_per_image=0.003, pricing_note=note),
            ModelCapabilities("fal-ai/flux/dev", "FLUX dev (fal)", seed=True, async_jobs=True, max_side=2048,
                              aspect_ratios=["1:1", "3:4", "4:3"], price_per_image=0.025, pricing_note=note),
            ModelCapabilities("fal-ai/flux-pro/v1.1", "FLUX1.1 pro (fal)", seed=True, async_jobs=True, max_side=2048,
                              aspect_ratios=["1:1", "3:4", "4:3"], price_per_image=0.04, pricing_note=note),
        ]

    def _headers(self) -> dict:
        return {"Authorization": f"Key {self.require_key()}", "Content-Type": "application/json"}

    @wrap_transport_errors("fal")
    def generate_image(self, req: GenerationRequest) -> GenerationResult:
        p = self.map_request(req)
        body = {"prompt": req.prompt, "image_size": {"width": p["width"], "height": p["height"]},
                "num_images": 1, "output_format": "png", "enable_safety_checker": True}
        if "seed" in p:
            body["seed"] = p["seed"]
        t0 = time.monotonic()
        with self.client(req.timeout_seconds) as c:
            r = c.post(f"{self.QUEUE}/{req.model_id}", json=body, headers=self._headers())
            if r.status_code >= 400:
                raise http_error("fal", r)
            sub = r.json()

            def check():
                sr = c.get(sub["status_url"], headers=self._headers())
                if sr.status_code >= 400:
                    raise http_error("fal", sr)
                st = sr.json().get("status")
                if st == "COMPLETED":
                    return True
                if st in ("FAILED", "ERROR"):
                    raise ProviderError("fal: request failed", provider="fal")
                return None
            poll_until(check, timeout=req.timeout_seconds, interval=float(self.settings.get("poll_interval", 1.5)),
                       provider="fal")
            rr = c.get(sub["response_url"], headers=self._headers())
            if rr.status_code >= 400:
                raise http_error("fal", rr)
            url = rr.json()["images"][0]["url"]
            img = c.get(url).content
        w, h = _dims(img)
        return GenerationResult(image=img, mime="image/png", provider_id=self.provider_id, model_id=req.model_id,
                                effective_params={k: v for k, v in body.items() if k != "prompt"}, width=w, height=h,
                                provider_job_id=sub.get("request_id"), cost=self.estimate_cost(req).amount,
                                cost_is_estimate=True, duration_seconds=time.monotonic() - t0)

    def validate_configuration(self) -> ValidationResult:
        if not self.api_key:
            return ValidationResult(False, "No API key configured")
        if ":" not in self.api_key:
            return ValidationResult(False, "fal keys are usually of the form '<id>:<secret>'")
        return ValidationResult(True, "Key stored; fal has no free auth check — first generation verifies it",
                                [m.model_id for m in self.list_models()])
