"""Wikidata: what a place is called in Russian, what to say about it, and whether it matters.

OpenStreetMap tells us a museum exists at a coordinate; it rarely carries a description, and its
`name` is whatever language the mapper used. Wikidata holds the Russian label, the short
one-line description (`schema:description`) that the card shows, and a picture.

The same round trip doubles as the ranking signal. An OSM element with a `wikidata` tag has been
written about in a wikibase; one that also has a Russian Wikipedia article has been written about
*in Russian, by people who thought it worth an article*. Ordering the catalog by that is honest —
it measures attestation, not quality — and it is what keeps a city's list from being 400 random
benches.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from ingest.transport import IngestSourceError, PublicApi

logger = logging.getLogger(__name__)

#: SPARQL endpoints reject oversized queries; 50 ids keeps each one comfortably under the limit and
#: under the per-request cost ceiling the public endpoint enforces.
CHUNK = 50
RU_WIKI = "https://ru.wikipedia.org/"
_Q_ID = re.compile(r"Q\d+")


@dataclass(frozen=True)
class Entity:
    """What Wikidata knows about one Q-id, in Russian where Russian exists."""

    qid: str
    label: str | None = None
    description: str | None = None
    image: str | None = None
    has_ru_article: bool = False

    @property
    def source_url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.qid}"


def build_query(qids: tuple[str, ...]) -> str:
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""SELECT ?item ?label ?description ?image ?ruwiki WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item rdfs:label ?label FILTER (lang(?label) = "ru") }}
  OPTIONAL {{ ?item schema:description ?description FILTER (lang(?description) = "ru") }}
  OPTIONAL {{ ?item wdt:P18 ?image }}
  OPTIONAL {{ ?ruwiki schema:about ?item ; schema:isPartOf <{RU_WIKI}> }}
}}"""


async def fetch_entities(api: PublicApi, sparql_url: str, qids: set[str]) -> dict[str, Entity]:
    """Russian labels, descriptions and images for these Q-ids; a missing one is simply absent.

    Wikidata is the least critical link in the chain — without it we still have a name, a coordinate
    and a category — so a failed chunk degrades to "no enrichment for these" instead of failing the
    whole city.
    """
    ordered = sorted(qid for qid in qids if qid.startswith("Q"))
    found: dict[str, Entity] = {}
    for start in range(0, len(ordered), CHUNK):
        chunk = tuple(ordered[start : start + CHUNK])
        try:
            payload = await api.json(
                "GET",
                f"{sparql_url}?{urlencode_query(build_query(chunk))}",
                headers={"Accept": "application/sparql-results+json"},
            )
        except IngestSourceError as error:
            logger.warning("Wikidata chunk of %d failed: %s", len(chunk), error)
            continue
        found.update(_parse(payload))
    return found


def urlencode_query(query: str) -> str:
    return "query=" + quote(query, safe="")


def _parse(payload: Any) -> dict[str, Entity]:
    rows = ((payload or {}).get("results") or {}).get("bindings") or []
    merged: dict[str, Entity] = {}
    for row in rows:
        qid = _value(row.get("item"))
        if not qid:
            continue
        key = _qid_of(qid)
        if not key:
            continue
        previous = merged.get(key)
        image = _file_name(_value(row.get("image")))
        merged[key] = Entity(
            qid=key,
            label=_value(row.get("label")) or (previous.label if previous else None),
            description=_value(row.get("description")) or (previous.description if previous else None),
            image=image or (previous.image if previous else None),
            has_ru_article=bool(_value(row.get("ruwiki"))) or bool(previous and previous.has_ru_article),
        )
    return merged


def _value(cell: Any) -> str | None:
    if isinstance(cell, dict):
        value = cell.get("value")
        return str(value) if value else None
    return None


def _qid_of(reference: str) -> str:
    """`http://www.wikidata.org/entity/Q123` to `Q123`.

    SPARQL results carry the full URI while the OSM tag carries the bare id, and the two have to be
    comparable for the lookup to hit at all. Anything that is not a Q-id is not a Wikidata entity.
    """
    candidate = reference.rsplit("/", 1)[-1].strip()
    return candidate if _Q_ID.fullmatch(candidate) else ""


def _file_name(image_url: str | None) -> str | None:
    """A P18 answer to the Commons title the API takes: `File:Kazan Kremlin.jpg`.

    SPARQL hands back a page address rather than the file, in one of two spellings: `…/wiki/File:…`
    and, as the endpoint actually answers today, the redirect-style `…/wiki/Special:FilePath/…` with
    the name percent-encoded. Both end at the same title, and the title is written with spaces and
    not with underscores, because that is the form the Commons API returns in `pages[].title` — and
    the picture lookup compares the two strings.
    """
    if not image_url:
        return None
    marker = "Special:FilePath/"
    index = image_url.find(marker)
    if index == -1:
        decoded = unquote(image_url)
        at = decoded.find("File:")
        if at == -1:
            return None
        tail = decoded[at + len("File:") :]
    else:
        tail = image_url[index + len(marker) :]
    name = unquote(tail.split("#", 1)[0].split("?", 1)[0]).replace("_", " ").strip()
    return f"File:{name}" if name else None
