"""API routers mounted under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers import places, routes, saved_routes

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(places.router)
# `routes` and `saved_routes` share the /routes prefix on purpose. The overlap is harmless — Starlette
# prefers a full method match over a partial one, so POST /routes/generate still reaches the generator
# whichever router is included first. Only GET /routes/generate means anything else: an integer id.
api_router.include_router(routes.router)
api_router.include_router(saved_routes.router)

__all__ = ["api_router"]
