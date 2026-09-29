"""API routers mounted under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers import guides, ingest, places, routes, saved_routes

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(places.router)
# `guides` answers `/places/{place_id}/guide` and `/guides`; it is a second router over the same
# objects because a guide is a lazy, hand-written payload that no listing has to carry.
api_router.include_router(guides.router)
# `routes` and `saved_routes` share the /routes prefix on purpose. The overlap is harmless — Starlette
# prefers a full method match over a partial one, so POST /routes/generate still reaches the generator
# whichever router is included first. Only GET /routes/generate means anything else: an integer id.
api_router.include_router(routes.router)
api_router.include_router(saved_routes.router)
# The only write endpoint in the API, and the only one behind a token: it refills `data/places.d/`.
api_router.include_router(ingest.router)

__all__ = ["api_router"]
