"""Provider adapter contract tests (MOCKED HTTP — no network, no charges) and Test A (LIVE, opt-in).

The contract tests check that each adapter builds the request its documented
API expects, drops unsupported parameters, parses responses, and maps
errors to typed, retry-aware exceptions. They do NOT prove the live service
accepts the request; that is what the `live` tests are for.
"""
import base64
import io
import json
import os

import httpx
import pytest
from PIL import Image

from studio.providers import base
from studio.providers.bfl import BFLProvider
from studio.providers.google import GoogleImageProvider
from studio.providers.hosted import FalProvider, ReplicateProvider
from studio.providers.openai_images import OpenAIImageProvider
from studio.providers.stability import StabilityProvider


def png(w=64, h=80) -> bytes:
    b = io.BytesIO()
    Image.new("L", (w, h), 255).save(b, "PNG")
    return b.getvalue()


def req(provider, model, **kw):
    return base.GenerationRequest("p", "pg", None, provider, model, "a turtle", negative_prompt="text, shading",
                                  width=2250, height=2670, seed=42, **kw)


class Recorder:
    def __init__(self, handler):
        self.calls = []
        self.handler = handler

    def __call__(self, request: httpx.Request):
        self.calls.append(request)
        return self.handler(request)


def test_openai_contract():
    def h(r):
        assert r.headers["authorization"] == "Bearer sk-test-123456789"
        body = json.loads(r.content)
        assert body["model"] == "gpt-image-1" and body["size"] == "1024x1536" and body["quality"] == "high"
        assert "negative_prompt" not in body and "seed" not in body  # unsupported -> not sent
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(png()).decode()}]})
    rec = Recorder(h)
    p = OpenAIImageProvider(api_key="sk-test-123456789", transport=httpx.MockTransport(rec))
    res = p.generate_image(req("openai", "gpt-image-1"))
    assert rec.calls[0].url.path == "/v1/images/generations"
    assert res.width == 1024 and res.height == 1536 and res.cost == 0.25 and res.cost_is_estimate
    assert "prompt" not in res.effective_params


@pytest.mark.parametrize("status,body,exc", [
    (401, {"error": {"message": "Incorrect API key"}}, base.ProviderAuthError),
    (429, {"error": {"message": "Rate limit reached"}}, base.ProviderRateLimited),
    (429, {"error": {"message": "You exceeded your current quota"}}, base.ProviderQuotaExceeded),
    (400, {"error": {"message": "Your request was rejected by the safety system"}}, base.ProviderContentRejected),
    (503, {"error": {"message": "overloaded"}}, base.ProviderError),
])
def test_error_mapping(status, body, exc):
    p = OpenAIImageProvider(api_key="sk-x-123456789", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json=body)))
    with pytest.raises(exc) as ei:
        p.generate_image(req("openai", "gpt-image-1"))
    assert ei.value.retryable == (status in (429, 503) and exc is not base.ProviderQuotaExceeded)


def test_timeout_is_retryable():
    def h(r):
        raise httpx.ReadTimeout("slow", request=r)
    p = OpenAIImageProvider(api_key="sk-x-123456789", transport=httpx.MockTransport(h))
    with pytest.raises(base.ProviderTimeout) as ei:
        p.generate_image(req("openai", "gpt-image-1"))
    assert ei.value.retryable


def test_google_gemini_and_imagen_contract():
    def h(r):
        assert r.headers["x-goog-api-key"] == "AIzaTEST1234567890"
        body = json.loads(r.content)
        if r.url.path.endswith(":generateContent"):
            assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
            assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "3:4"
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png()).decode()}}]}}]})
        assert r.url.path.endswith(":predict")
        assert body["parameters"]["aspectRatio"] == "3:4"
        return httpx.Response(200, json={"predictions": [{"bytesBase64Encoded": base64.b64encode(png()).decode()}]})
    p = GoogleImageProvider(api_key="AIzaTEST1234567890", transport=httpx.MockTransport(h))
    assert p.generate_image(req("google", "gemini-2.5-flash-image")).width == 64
    assert p.generate_image(req("google", "imagen-4.0-generate-001")).height == 80


def test_google_blocked_prompt():
    p = GoogleImageProvider(api_key="AIzaTEST1234567890", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})))
    with pytest.raises(base.ProviderContentRejected):
        p.generate_image(req("google", "gemini-2.5-flash-image"))


def test_bfl_async_contract():
    state = {"polls": 0}

    def h(r):
        if r.method == "POST":
            assert r.headers["x-key"] == "bfl-key"
            body = json.loads(r.content)
            assert body["width"] % 32 == 0 and body["height"] % 32 == 0 and body["seed"] == 42
            assert max(body["width"], body["height"]) <= 1440
            return httpx.Response(200, json={"id": "job1", "polling_url": "https://api.bfl.ai/v1/get_result?id=job1"})
        if "get_result" in str(r.url):
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"status": "Pending"})
            return httpx.Response(200, json={"status": "Ready", "result": {"sample": "https://cdn.example/x.png"}})
        return httpx.Response(200, content=png())
    p = BFLProvider(api_key="bfl-key", settings={"poll_interval": 0}, transport=httpx.MockTransport(h))
    res = p.generate_image(req("bfl", "flux-pro-1.1"))
    assert res.provider_job_id == "job1" and state["polls"] == 2


def test_bfl_moderated():
    def h(r):
        if r.method == "POST":
            return httpx.Response(200, json={"id": "j", "polling_url": "https://api.bfl.ai/v1/get_result?id=j"})
        return httpx.Response(200, json={"status": "Content Moderated"})
    p = BFLProvider(api_key="k", settings={"poll_interval": 0}, transport=httpx.MockTransport(h))
    with pytest.raises(base.ProviderContentRejected):
        p.generate_image(req("bfl", "flux-pro-1.1-ultra"))


def test_stability_contract():
    def h(r):
        assert r.url.path == "/v2beta/stable-image/generate/core"
        assert r.headers["accept"] == "image/*"
        assert b'name="negative_prompt"' in r.content and b'name="aspect_ratio"' in r.content
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
    p = StabilityProvider(api_key="sk-stab-123456789", transport=httpx.MockTransport(h))
    assert p.generate_image(req("stability", "stable-image-core")).mime == "image/png"


def test_replicate_contract():
    state = {"n": 0}

    def h(r):
        if r.method == "POST":
            assert r.url.path == "/v1/models/black-forest-labs/flux-schnell/predictions"
            assert json.loads(r.content)["input"]["aspect_ratio"] == "3:4"
            return httpx.Response(201, json={"id": "pr1", "status": "starting",
                                             "urls": {"get": "https://api.replicate.com/v1/predictions/pr1"}})
        if r.url.path == "/v1/predictions/pr1":
            state["n"] += 1
            return httpx.Response(200, json={"id": "pr1", "status": "succeeded",
                                             "output": ["https://replicate.delivery/x.png"],
                                             "urls": {"get": "https://api.replicate.com/v1/predictions/pr1"}})
        return httpx.Response(200, content=png())
    p = ReplicateProvider(api_key="r8_testtoken123", settings={"poll_interval": 0}, transport=httpx.MockTransport(h))
    res = p.generate_image(req("replicate", "black-forest-labs/flux-schnell"))
    assert res.provider_job_id == "pr1" and state["n"] == 1


def test_fal_contract():
    def h(r):
        u = str(r.url)
        if r.method == "POST":
            assert r.headers["authorization"] == "Key id:secret"
            return httpx.Response(200, json={"request_id": "f1", "status_url": "https://queue.fal.run/s/f1",
                                             "response_url": "https://queue.fal.run/r/f1"})
        if u.endswith("/s/f1"):
            return httpx.Response(200, json={"status": "COMPLETED"})
        if u.endswith("/r/f1"):
            return httpx.Response(200, json={"images": [{"url": "https://fal.media/x.png"}]})
        return httpx.Response(200, content=png())
    p = FalProvider(api_key="id:secret", settings={"poll_interval": 0}, transport=httpx.MockTransport(h))
    assert p.generate_image(req("fal", "fal-ai/flux/schnell")).provider_job_id == "f1"


def test_missing_key_is_clear():
    with pytest.raises(base.ProviderNotConfigured):
        OpenAIImageProvider().generate_image(req("openai", "gpt-image-1"))


def test_all_adapters_declare_capabilities_and_prices_honestly():
    for cls in (OpenAIImageProvider, GoogleImageProvider, BFLProvider, StabilityProvider, ReplicateProvider,
                FalProvider):
        for m in cls(api_key="x").list_models():
            assert m.model_id and m.pricing_note
            if m.price_per_image is None:
                assert "unknown" in m.pricing_note or "not verified" in m.pricing_note


def test_control_center_api_never_returns_keys(client):
    r = client.put("/api/providers/openai", json={"api_key": "sk-supersecret-abcdef123456", "default_model": "gpt-image-1"})
    assert r.status_code == 200
    body = r.text
    assert "supersecret" not in body and r.json()["key_masked"].endswith("3456")
    listing = client.get("/api/providers").text
    assert "supersecret" not in listing
    # stored encrypted at rest
    from studio.db import SessionLocal
    from studio.models import ProviderConfig
    with SessionLocal() as db:
        enc = db.get(ProviderConfig, "openai").encrypted_api_key
        assert "supersecret" not in enc
    # unknown model rejected, disable works
    assert client.put("/api/providers/openai", json={"default_model": "nope"}).status_code == 422
    r = client.put("/api/providers/openai", json={"enabled": False})
    assert r.json()["status"] == "disabled"


def test_control_center_test_connection_mock(client):
    r = client.post("/api/providers/mock/test")
    assert r.json() == {"ok": True, "message": r.json()["message"], "live": False}


LIVE = os.environ.get("STUDIO_LIVE_TESTS") == "1"


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="live provider tests need STUDIO_LIVE_TESTS=1 and real API keys (costs money)")
@pytest.mark.parametrize("cls,env,model", [
    (OpenAIImageProvider, "OPENAI_API_KEY", "gpt-image-1"),
    (GoogleImageProvider, "GOOGLE_API_KEY", "gemini-2.5-flash-image"),
    (BFLProvider, "BFL_API_KEY", "flux-pro-1.1"),
    (StabilityProvider, "STABILITY_API_KEY", "stable-image-core"),
    (ReplicateProvider, "REPLICATE_API_TOKEN", "black-forest-labs/flux-schnell"),
    (FalProvider, "FAL_KEY", "fal-ai/flux/schnell"),
])
def test_A_live_provider_smoke(cls, env, model):
    key = os.environ.get(env)
    if not key:
        pytest.skip(f"{env} not set")
    p = cls(api_key=key)
    v = p.validate_configuration()
    assert v.ok, v.message
    res = p.generate_image(base.GenerationRequest("live", None, None, p.provider_id, model,
                                                  "Black-and-white coloring page of a friendly turtle, clean outlines",
                                                  width=1024, height=1024, quality="low"))
    assert res.image and res.width > 0
