"""Operator endpoints: start a city pass and ask how the last one went.

Writing the catalog is the one thing in this API that changes data rather than reading it, so it is
the one place a token is required, and the failure modes are separated on purpose: no token
configured is the feature being off (503), a wrong token is an unauthenticated caller (401), and a
run already going is a conflict (409) rather than a silently queued second fetch.

The answer arrives before the work does. `ingest_cities` spends tens of seconds per city waiting on
OpenStreetMap, and an HTTP request that blocks for that long is a timeout waiting to happen on the
way through nginx — so the endpoint starts the job and points at `GET /admin/ingest/status`.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException

from ingest.job import job
from ingest.taxonomy import CATEGORIES
from server.schemas import (
    INGEST_BUSY_RESPONSE,
    INGEST_DISABLED_RESPONSE,
    INGEST_UNAUTHORIZED_RESPONSE,
    IngestAccepted,
    IngestRequest,
    IngestStatus,
)

router = APIRouter(tags=["Ingest"])

TOKEN_HEADER = "X-Ingest-Token"


def _authorize(token: str | None) -> None:
    if not job.settings.ingest_open:
        raise HTTPException(status_code=503, detail=INGEST_DISABLED_RESPONSE["description"])
    presented = (token or "").strip()
    # Constant-time on purpose: the value is compared against a secret, and a timing oracle over an
    # internal admin endpoint is still a timing oracle.
    if not presented or not secrets.compare_digest(presented, job.settings.ingest_token.strip()):
        raise HTTPException(status_code=401, detail=INGEST_UNAUTHORIZED_RESPONSE["description"])


@router.post(
    "/admin/ingest",
    response_model=IngestAccepted,
    status_code=202,
    summary="Запустить поиск мест для города",
    description=(
        "Ищет реальные места в OpenStreetMap, берёт описания и фотографии из Wikidata и Commons и "
        "перезаписывает `data/places.d/<город>.json`. Отвечает сразу, не дожидаясь конца: прогресс — "
        "в `GET /admin/ingest/status`. Требует заголовок `" + TOKEN_HEADER + "`. "
        "Категории, которыми работает сборщик: " + ", ".join(CATEGORIES) + "."
    ),
    responses={
        401: INGEST_UNAUTHORIZED_RESPONSE,
        409: INGEST_BUSY_RESPONSE,
        503: INGEST_DISABLED_RESPONSE,
    },
)
async def start_ingest(
    payload: IngestRequest,
    x_ingest_token: str | None = Header(None, alias=TOKEN_HEADER),
) -> IngestAccepted:
    _authorize(x_ingest_token)
    started = job.start(payload.cities, limit=payload.limit, dry_run=payload.dry_run)
    if not started:
        raise HTTPException(
            status_code=409,
            detail=(
                "Идёт обработка "
                + ", ".join(job.requested)
                + " — дождитесь окончания через GET /api/v1/admin/ingest/status"
            ),
        )
    return IngestAccepted(
        status="started", cities=payload.cities, running_since=job.started_at
    )


@router.get(
    "/admin/ingest/status",
    response_model=IngestStatus,
    summary="Состояние сборщика мест",
    description=(
        "Идёт ли прогон сейчас и чем закончился последний. Тот же `" + TOKEN_HEADER + "`, что и для "
        "запуска: отчёт показывает, какие города и откуда были собраны."
    ),
    responses={
        401: INGEST_UNAUTHORIZED_RESPONSE,
        503: INGEST_DISABLED_RESPONSE,
    },
)
async def ingest_status(
    x_ingest_token: str | None = Header(None, alias=TOKEN_HEADER),
) -> IngestStatus:
    _authorize(x_ingest_token)
    return IngestStatus.model_validate(job.status())
