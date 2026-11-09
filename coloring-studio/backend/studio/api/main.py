"""FastAPI application factory.

Run: uvicorn studio.api.main:app --reload  (from the backend directory)
"""
from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import migrate
from ..jobs import Worker
from .routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(start_worker: bool | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Coloring Book Publishing Studio", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    worker = Worker()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        token = settings.api_token
        path = request.url.path
        if token and path.startswith("/api") and path != "/api/health" and request.method != "OPTIONS":
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
                request.query_params.get("token", "")
            if not hmac.compare_digest(given, token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.on_event("startup")
    def _startup():
        migrate()
        run = settings.run_worker if start_worker is None else start_worker
        if run:
            worker.start()

    @app.on_event("shutdown")
    def _shutdown():
        worker.stop()

    app.include_router(router)
    return app


app = create_app()
