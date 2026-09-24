"""API routers mounted under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers import places, routes

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(places.router)
api_router.include_router(routes.router)

__all__ = ["api_router"]
