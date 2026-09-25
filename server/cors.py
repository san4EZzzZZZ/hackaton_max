"""CORS wiring so the Mini App can call /api/v1 from a browser webview.

The allowlist is read from the environment instead of `core.config.Settings`, because that module
belongs to the bot half of the repo. The wildcard default is honest: no endpoint authenticates with
cookies, so there is no credentialed cross-origin request to protect.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.schemas import PAGINATION_HEADERS

ENV_VAR = "CORS_ALLOW_ORIGINS"

# Only the methods the API actually declares; /webhook is server-to-server and never preflighted.
ALLOWED_METHODS = ["GET", "POST", "OPTIONS"]

# A cross-origin fetch hides every response header outside the CORS-safelisted set unless the server
# names it here, so a documented header the client cannot read is a silent contract break. Derived from
# the spec's own header block to keep the two from drifting apart again.
EXPOSED_HEADERS = list(PAGINATION_HEADERS)

# Preflight cache, seconds. Origins change on deploy, not per request.
MAX_AGE = 600


def allowed_origins() -> list[str]:
    """Origins from `CORS_ALLOW_ORIGINS` (comma separated), falling back to the wildcard."""
    value = os.environ.get(ENV_VAR, "")
    origins = [origin.strip() for origin in value.split(",") if origin.strip()]
    return origins or ["*"]


def configure_cors(app: FastAPI) -> FastAPI:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=ALLOWED_METHODS,
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
        max_age=MAX_AGE,
    )
    return app
