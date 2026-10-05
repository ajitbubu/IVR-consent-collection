from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fastapi.middleware.cors import CORSMiddleware

from app.api import health, router as service_router
from app.console_api import router as console_router
from app.identity import PhoneNormalisationError
from app.routes_exotel import router as exotel_router
from app.routes_sprinklr import router as sprinklr_router
from app.routes_sprinklr_oauth import router as sprinklr_oauth_router
from app.routes_twilio import router as twilio_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def create_app() -> FastAPI:
    app = FastAPI(
        title="IVR Consent Capture",
        version="0.1.0",
        description=(
            "Captures DPDP consent over Exotel IVR, stores it as the "
            "write-ahead record, and pushes it to UCM asynchronously."
        ),
    )
    # In production the provider routers are bound to a different hostname
    # from the service API. The Exotel one sits behind an IP allowlist and
    # returns no data, because Exotel cannot sign its requests; the Twilio
    # one authenticates every request by signature instead, and the Sprinklr
    # one by a shared bearer token.
    app.include_router(exotel_router)
    app.include_router(twilio_router)
    app.include_router(sprinklr_router)
    app.include_router(sprinklr_oauth_router)
    app.include_router(service_router)
    app.include_router(console_router)
    app.include_router(health)

    # A number that cannot be normalised is bad input on every route that
    # takes one -- the service API, the console search and the webhooks.
    @app.exception_handler(PhoneNormalisationError)
    def bad_phone(_request: Request, exc: PhoneNormalisationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Dev only: the console runs on the Vite port. In production the built
    # console is served as static files from the same origin.
    origins = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=True,
            allow_methods=["*"], allow_headers=["*"],
        )

    mount_console(app)
    return app


def mount_console(app: FastAPI) -> None:
    """Serve the built admin console, when it has been built."""
    dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if not dist.is_dir():
        return

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/console/assets", StaticFiles(directory=dist / "assets"), name="console-assets")

    @app.get("/console", include_in_schema=False)
    @app.get("/console/{path:path}", include_in_schema=False)
    def console_spa(path: str = "") -> FileResponse:
        """Deep links like /console/consents/01ABC are client-side routes, so
        anything that is not a real asset returns the app shell."""
        candidate = dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


app = create_app()
