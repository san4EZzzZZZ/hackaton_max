"""Wikimedia Commons: a card-sized picture for a place, and who to credit for it.

Wikidata's `P18` names a file; it does not hand over a usable picture. Asking Commons for a 960 px
thumbnail gives the client something that is not a 12 MB original, and the same call returns the
license and the author, which Commons requires us to be able to show. The catalog serves the
thumbnail URL directly — the app never proxies images and never needs an outbound request to answer
a `/places` call, exactly as it did not for the hand-picked seed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlencode, urlsplit

from ingest.transport import IngestSourceError, PublicApi

logger = logging.getLogger(__name__)

#: Commons takes up to 50 titles per query; going wider is a longer answer for no fewer round trips.
CHUNK = 50
#: 960 px is what the seed catalog uses, and what a phone card is drawn at on a 2x screen.
THUMB_WIDTH = 960


@dataclass(frozen=True)
class Picture:
    url: str
    license: str | None = None
    author: str | None = None


async def fetch_thumbs(api: PublicApi, commons_url: str, titles: set[str]) -> dict[str, Picture]:
    """`File:…` titles mapped to a served thumbnail; a file that is gone is simply not in the answer."""
    ordered = sorted(title for title in titles if title)
    found: dict[str, Picture] = {}
    for start in range(0, len(ordered), CHUNK):
        batch = ordered[start : start + CHUNK]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(batch),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": str(THUMB_WIDTH),
        }
        try:
            payload = await api.json(
                "GET",
                f"{commons_url.rstrip('/')}?{urlencode(params)}",
                headers={"Accept": "application/json"},
            )
        except IngestSourceError as error:
            logger.warning("Commons batch of %d failed: %s", len(batch), error)
            continue
        found.update(_parse(payload))
    return found


def _parse(payload: Any) -> dict[str, Picture]:
    pages = ((payload or {}).get("query") or {}).get("pages") or {}
    out: dict[str, Picture] = {}
    for page in pages.values():
        if not isinstance(page, dict) or page.get("missing") is not None:
            continue
        title = page.get("title")
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not title or not url or not str(url).startswith("https://"):
            continue
        meta = info.get("extmetadata") or {}
        out[str(title)] = Picture(
            url=_served_from(url),
            license=_text(meta.get("LicenseShortName")),
            author=_text(meta.get("Artist")),
        )
    return out


def _text(cell: Any) -> str | None:
    value = (cell or {}).get("value") if isinstance(cell, dict) else None
    if not value:
        return None
    # `Artist` arrives as an HTML fragment with the name inside an <a>; the credit line wants text.
    cleaned = " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value))).split()).strip(" -|/")
    return cleaned[:160] or None


def _served_from(url: str) -> str:
    """Rewrite the resolved thumbnail onto the host the catalog already links to.

    Commons hands out `upload.wikimedia.org` addresses while `data/places.json` is full of
    `thumb.wikimedia.org`, which serves the identical paths. They are the same CDN, so the choice is
    about keeping one shape in one file — a client that caches or filters by host sees one pattern.
    """
    parts = urlsplit(url)
    if parts.netloc == "upload.wikimedia.org" and "/wikipedia/commons/thumb/" in parts.path:
        return "https://thumb.wikimedia.org" + parts.path
    return url
