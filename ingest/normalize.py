"""Refining raw OSM/Wikidata answers into catalog records a visitor can be sent to.

Three judgement calls live here, and they are the whole reason this module is not a `for` loop.

**Ids are deterministic.** `kazan-kremlin` is derived from the title, not from the OSM element id,
because a route saved against an id has to survive a re-fetch of the city, and because the
hand-written guides in `data/place_guides.json` reference places by that slug. A second object with
the same slug gets `-2`, and the numbering is done after sorting by title so one run agrees with the
next.

**A rating is earned, not invented.** `Place.rating` drives `min_rating` filters, so a constant 5.0
would make that filter meaningless and every generated city would look identical. What we actually
know is how well attested an object is — a Russian Wikipedia article, a Wikidata item, a photo,
opening hours. The number is a monotone function of that, and the record says plainly in
`provenance.unverified_fields` that the rating is an inference rather than a survey.

**Nothing is claimed to be verified that was not.** Price, Pushkin-card acceptance and visitor
durations are not in OpenStreetMap. They fall back to a category default, and each one is named in
`unverified_fields` exactly the way the curated seed does it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ingest.commons import Picture
from ingest.geo import City
from ingest.osm import Element
from ingest.taxonomy import CAFE, PARK, Interest, tags_of, visit_minutes
from ingest.wikidata import Entity
from server.routing import haversine_km
from server.schemas import Location, Place, TagId

logger = logging.getLogger(__name__)

#: Two objects this close and this similarly named are the same place mapped twice: OSM carries a
#: node and a building way for plenty of museums, and the catalog must not show both.
DUPLICATE_METRES = 90
#: A chain is one entry per brand per city: fifteen branches of the same coffee house is one place
#: with fifteen doors.
MAX_PER_BRAND = 1

_DAYS_RU = {"Mo": "пн", "Tu": "вт", "We": "ср", "Th": "чт", "Fr": "пт", "Sa": "сб", "Su": "вс"}
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


@dataclass
class Candidate:
    """One place, the evidence behind it, and how strongly that evidence ranks it."""

    place: Place
    provenance: dict[str, Any]
    score: float
    element: Element
    brand: str | None = None
    dropped_reason: str | None = field(default=None, init=False)


def build_candidates(
    city: City,
    grouped: dict[str, tuple[Element, ...]],
    entities: dict[str, Entity],
    pictures: dict[str, Picture],
    *,
    core_radius_km: float | None = None,
) -> list[Candidate]:
    """Refine every element the box returned, then keep the copies of a place that are not the same place.

    `core_radius_km` is the clip the bounding box cannot be trusted to give: Nominatim answers a
    rectangle, and the rectangle around Sochi runs down the coast past the border and up into the
    mountains — the measured tail was a waterfall at 31 km and a viaduct at 48. Those objects are real
    and they are not stops on a walk through this city, so they leave here rather than being published
    and then never picked.
    """
    out: list[Candidate] = []
    beyond_core = 0
    for category, elements in grouped.items():
        for element in elements:
            if core_radius_km is not None and _beyond_core(city, element, core_radius_km):
                beyond_core += 1
                continue
            candidate = _one(city, category, element, entities, pictures)
            if candidate is not None:
                out.append(candidate)
    if beyond_core:
        logger.info(
            "%s: %d объектов дальше %.0f км от центра не вошли в каталог",
            city.name,
            beyond_core,
            core_radius_km,
        )
    return dedupe(out)


def _beyond_core(city: City, element: Element, core_radius_km: float) -> bool:
    return haversine_km(city_location(city), element_location(element)) > core_radius_km


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Keep the best copy of every real-world place.

    Keyed first on the Wikidata id — which is a statement that these are the same object, not a
    guess from coordinates — and then on the coordinate-and-name pair for the majority of places
    that have no wiki link at all.
    """
    best: dict[tuple, Candidate] = {}
    by_qid: dict[str, Candidate] = {}
    for candidate in candidates:
        qid = candidate.element.wikidata
        if qid and (previous := by_qid.get(qid)):
            if candidate.score > previous.score:
                best[_key(previous)] = candidate
                by_qid[qid] = candidate
            continue
        key = _key(candidate)
        if (previous := best.get(key)) is None or candidate.score > previous.score:
            best[key] = candidate
            if qid:
                by_qid[qid] = candidate
    return list(best.values())


def _key(candidate: Candidate) -> tuple:
    place = candidate.place
    lat = round(place.location.lat * 100000 / (DUPLICATE_METRES / 10))
    lon = round(place.location.lon * 100000 / (DUPLICATE_METRES / 10))
    return (lat, lon, place.title.lower())


#: Quotes, case and punctuation carry no identity in a title: «Дом Врангеля» and Дом Врангеля are one
#: building, and the seed writes them both ways.
_TITLE_NOISE = re.compile(r"[^0-9a-zа-яё]+")


def title_key(title: str) -> str:
    """The shape a title is compared in across catalogs."""
    return _TITLE_NOISE.sub(" ", title.casefold()).strip()


def fit_to_publish(
    candidates: list[Candidate], *, known_titles: frozenset[str] = frozenset()
) -> list[Candidate]:
    """Drop what would open as an empty card, and what the seed already has under another id.

    `dedupe` cannot do either of these: it compares the run's own objects, and the hand-written seed is
    not among them. Both failures end on the same screen, which is why they live in one filter.

    A place with neither a picture nor a sentence is a stop the visitor walks to and then reads
    nothing about; the Rostov pass measured this directly — 147 objects found, 2 of them able to fill a
    card. And a building the seed already publishes arrives with a different id, so nothing else stops
    it standing on the map twice, one metre from its own copy.
    """
    kept: list[Candidate] = []
    for candidate in candidates:
        place = candidate.place
        if title_key(place.title) in known_titles:
            candidate.dropped_reason = "already in the seed"
            continue
        if not ((place.description or "").strip() or (place.image_url or "").strip()):
            candidate.dropped_reason = "no picture and no description"
            continue
        kept.append(candidate)
    return kept


def select(
    candidates: list[Candidate],
    interests: tuple[Interest, ...],
    *,
    total_limit: int,
    per_category: int | None = None,
) -> list[Candidate]:
    """Trim to `per_category` (or the interest's own cap) and then to `total_limit`, best first.

    The per-category cap comes before the total one: a city with 300 monuments and two museums
    should still show its museums, which a plain global top-N would quietly drop.

    The total limit is then taken one object per category at a time, in the same score order, rather
    than as a global top-N. It has to be: `Кафе` is a category whose objects almost never carry a
    Wikidata item or a photo, so ranked against cathedrals it loses every slot, and the live Kazan
    run of the balanced selection published a single coffee house — one chip, one stop. Ten decent
    cafés and two fewer monuments is the better catalog for anyone filtering by mood.
    """
    groups: list[list[Candidate]] = []
    for interest in interests:
        pool = sorted(
            (c for c in candidates if c.place.category == interest.category),
            key=lambda c: (-c.score, c.place.title),
        )
        cap = per_category if per_category is not None else interest.limit
        groups.append(_capped(pool, interest.category, cap)[:cap])

    kept: list[Candidate] = []
    rank = 0
    while len(kept) < total_limit and any(len(group) > rank for group in groups):
        for group in groups:
            if rank < len(group):
                kept.append(group[rank])
                if len(kept) == total_limit:
                    break
        rank += 1
    kept.sort(key=lambda c: (-c.score, c.place.title))
    return kept


def _capped(pool: list[Candidate], category: str, cap: int) -> list[Candidate]:
    """One door per brand, and both moods of a mixed category inside the cap.

    The brand rule is the obvious one: fifteen branches of the same coffee house are one place with
    fifteen doors. The second half of this function is not about tidiness — `Кафе` answers two
    different requests, «взять кофе» and «перекусить», and ranked by attestation alone a city's
    twelve café slots fill with restaurants. The coffee chip was left with one place in Kazan, which
    is a chip whose route is a single stop. So the two groups alternate down the list, each in its own
    score order, and the cap cuts whatever the alternation produced.
    """
    if category != CAFE:
        return pool
    by_brand: list[Candidate] = []
    seen: dict[str, int] = {}
    for candidate in pool:
        brand = candidate.brand
        if brand and seen.get(brand, 0) >= MAX_PER_BRAND:
            continue
        seen[brand] = seen.get(brand, 0) + 1
        by_brand.append(candidate)
    coffee = [candidate for candidate in by_brand if "coffee" in candidate.place.tags]
    other = [candidate for candidate in by_brand if "coffee" not in candidate.place.tags]
    interleaved: list[Candidate] = []
    for index in range(max(len(coffee), len(other))):
        for group in (coffee, other):
            if index < len(group):
                interleaved.append(group[index])
    return interleaved


def assign_ids(candidates: list[Candidate]) -> list[Candidate]:
    """Stable, human-readable ids, kept attached to their evidence.

    Sorted by category and title first so the numbering — and therefore the ids an editor writes
    guides against — does not depend on the order the queries happened to answer in.
    """
    taken: set[str] = set()
    numbered: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: (c.place.category, c.place.title.lower())):
        base = slugify(candidate.place.title)
        slug, suffix = base, 2
        while slug in taken:
            slug = f"{base}-{suffix}"
            suffix += 1
        taken.add(slug)
        candidate.place = candidate.place.model_copy(update={"id": slug})
        numbered.append(candidate)
    return numbered


def slugify(title: str) -> str:
    """`Казанский кремль` to `kazanskiy-kreml`.

    Latin ids are not a taste decision: the slug appears in URLs, in the guide file an editor writes
    by hand, and in the client's saved routes, and a Cyrillic slug is percent-encoded noise in two of
    the three. The letter table already yields «ий» for «ий» and drops the soft sign, so no special
    cases are needed — and none should be added, or the same title would hash to two ids.
    """
    characters = [_slug_char(char) for char in title.lower()]
    slug = re.sub(r"-+", "-", "".join(characters)).strip("-")
    return slug[:60] or "place"


def _slug_char(char: str) -> str:
    if char in _TRANSLIT:
        return _TRANSLIT[char]
    if char.isascii() and char.isalnum():
        return char
    return "-"


def humanize_hours(opening_hours: str | None) -> str | None:
    """`Mo-Fr 09:00-18:00; Sa 10:00-14:00` to `пн–пт 09:00–18:00, сб 10:00–14:00`.

    The tag is written for a machine: `24/7`, `Fr off`, `Sa[-1] 10:00-14:00`. Anything that is not a
    plain day range and a time range is dropped rather than paraphrased, because a card that says
    «пн» when the source said «Sa[-1] off» is worse than a card that says nothing.
    """
    if not opening_hours:
        return None
    text = opening_hours.strip()
    if text in {"24/7", "24x7", "open 24/7"}:
        return "круглосуточно"
    pieces: list[str] = []
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule or "off" in rule or "[" in rule or " " not in rule:
            continue
        days, _, times = rule.partition(" ")
        rendered_days = _render_days(days)
        rendered_times = times.replace("-", "–")
        if rendered_days is None:
            continue
        pieces.append(f"{rendered_days} {rendered_times}".strip())
    return ", ".join(pieces) or None


def _render_days(spec: str) -> str | None:
    parts: list[str] = []
    for chunk in spec.split(","):
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            if start not in _DAYS_RU or end not in _DAYS_RU:
                return None
            parts.append(f"{_DAYS_RU[start]}–{_DAYS_RU[end]}")
        elif chunk in _DAYS_RU:
            parts.append(_DAYS_RU[chunk])
        elif chunk in {"Mo-Su", "Daily", "everyday"}:
            parts.append("ежедневно")
        else:
            return None
    return ",".join(parts) or None


def _one(
    city: City,
    category: str,
    element: Element,
    entities: dict[str, Entity],
    pictures: dict[str, Picture],
) -> Candidate | None:
    tags = element.tags
    entity = entities.get(element.wikidata or "")
    title = _title(tags, entity)
    if not title:
        return None

    distance_km = haversine_km(city_location(city), element_location(element))
    picture = _picture(tags, entity, pictures)
    score = _score(tags, entity, picture, distance_km, city)
    unverified = _unverified(tags, entity, picture)
    hours = humanize_hours(tags.get("opening_hours"))
    image_url = picture.url if picture else None

    place = Place(
        id="pending",  # replaced by assign_ids once the whole city is known
        title=title,
        description=_description(tags, entity),
        category=category,
        tags=_tags(category, tags),
        location=element_location(element),
        address=_address(tags, city),
        city=city.name,
        is_pushkin_card=False,
        price=_price(tags),
        working_hours=hours,
        rating=_rating(score),
        visit_duration_minutes=_duration(category, tags),
        image_url=image_url,
    )
    provenance = {
        "source_url": (entity.source_url if entity else element.source_url),
        "osm_url": element.source_url,
        "last_verified": date.today().isoformat(),
        "unverified_fields": unverified,
        # How the number was reached, stated rather than implied: filters read these fields.
        "rating_basis": "attestation" if not tags.get("rating") else "osm:rating",
        "distance_from_center_km": round(distance_km, 2),
    }
    if picture and (picture.license or picture.author):
        provenance["image_credit"] = {
            "author": picture.author,
            "license": picture.license,
            "file": picture_title(tags, entity),
        }
    return Candidate(
        place=place,
        provenance=provenance,
        score=score,
        element=element,
        brand=tags.get("brand") or tags.get("operator"),
    )


def _title(tags: dict[str, str], entity: Entity | None) -> str | None:
    for value in (tags.get("name:ru"), tags.get("name"), entity.label if entity else None):
        if value and value.strip():
            return value.strip()[:200]
    return None


def _description(tags: dict[str, str], entity: Entity | None) -> str | None:
    """OSM prose first, Wikidata's one-liner second.

    A mappers' `description` is usually a sentence about the place; `schema:description` is a
    taxonomy line («музей в Татарстане») that reads badly on a card but beats showing nothing.
    """
    osm_text = (tags.get("description") or "").strip()
    if len(osm_text) >= 40:
        return osm_text[:600]
    if entity and entity.description:
        return f"{entity.description.strip().rstrip('.')}."[:600]
    return osm_text[:600] or None


def _address(tags: dict[str, str], city: City) -> str | None:
    full = (tags.get("addr:full") or "").strip()
    if full:
        return full[:200]
    street = (tags.get("addr:street") or "").strip()
    number = (tags.get("addr:housenumber") or "").strip()
    if street:
        return f"{street}{', ' + number if number else ''}, {city.name}"[:200]
    place = (tags.get("addr:place") or "").strip()
    if place:
        return f"{place}, {city.name}"[:200]
    return city.name


def _tags(category: str, tags: dict[str, str]) -> list[TagId]:
    """The interest's mood tags, narrowed by what the object actually is.

    `Кафе` covers a coffee house and a full restaurant, and the chips are supposed to separate «кофе»
    from «перекусить» — which the seed already does by hand. Same axis, same rule, from `amenity`.
    """
    if category != CAFE:
        return list(tags_of(category))
    amenity = tags.get("amenity") or ""
    if amenity in {"restaurant", "bar", "pub", "fast_food"}:
        return ["food"]
    if amenity in {"cafe", "coffee_shop", "confectionery", "pastry", "bakery", "ice_cream"}:
        return ["coffee"]
    return list(tags_of(category))


def _duration(category: str, tags: dict[str, str]) -> int:
    minutes = visit_minutes(category)
    if category == PARK:
        area_m2 = _number(tags.get("area")) or _number(tags.get("landuse_area"))
        if area_m2 and area_m2 > 200_000:
            minutes = 90
        elif area_m2 and area_m2 < 10_000:
            minutes = 25
    return max(15, minutes)


def _price(tags: dict[str, str]) -> float:
    for key in ("charge", "fee"):
        value = _number(tags.get(key))
        if value is not None and value > 0:
            return float(value)
    return 0.0


def _number(value: str | None) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", value or "")
    return float(match.group().replace(",", ".")) if match else None


def _rating(score: float) -> float:
    """Attestation mapped onto the band the hand-written catalog lives in (4.2–4.8).

    The divisor is what keeps the band honest: a city's evidence sum runs from about −1 (an unnamed
    field on the edge of the box) to about 7 (article, photo, hours, attraction, heritage), so it is
    spread over the six tenths rather than clamped — dividing by four parked every place with a
    Russian article on 4.8 and made `min_rating` filter nothing out.
    """
    return round(min(4.8, max(4.2, 4.2 + score / 8.0)), 1)


def _score(
    tags: dict[str, str],
    entity: Entity | None,
    picture: Picture | None,
    distance_km: float,
    city: City,
) -> float:
    """Evidence, summed. Every term is something a person could point at.

    Deliberately not a popularity score: nothing here knows whether people like the place, only
    whether the world has written it down.
    """
    score = 0.0
    if entity is not None:
        score += 1.0
    if entity and entity.has_ru_article:
        score += 1.5
    if picture:
        score += 0.7
    if tags.get("opening_hours"):
        score += 0.4
    if tags.get("website") or tags.get("url"):
        score += 0.3
    if tags.get("tourism") == "attraction":
        score += 1.0
    if tags.get("historic"):
        score += 0.4
    if tags.get("protected_status") or tags.get("heritage"):
        score += 0.6
    if tags.get("name:ru"):
        score += 0.2
    if osm_rating := _number(tags.get("rating")):
        score += max(0.0, (osm_rating - 3.0) / 2.0)
    # A place an hour outside the center is a different trip, not a stop on a walk. The scale is the
    # city's own size, so «далеко» means 15 km in Kazan and 3 km in Suzdal.
    score -= min(1.5, (distance_km / city.half_extent_km) * 0.7)
    return score


def _unverified(tags: dict[str, str], entity: Entity | None, picture: Picture | None) -> list[str]:
    missing: list[str] = []
    if not _number(tags.get("rating")):
        missing.append("rating")
    if not any(tags.get(key) for key in ("charge", "fee")):
        missing.extend(["price"])
    missing.append("is_pushkin_card")
    missing.append("visit_duration_minutes")
    if not tags.get("opening_hours"):
        missing.append("working_hours")
    if not tags.get("addr:street") and not tags.get("addr:full"):
        missing.append("address")
    return missing


def picture_title(tags: dict[str, str], entity: Entity | None) -> str | None:
    """The Commons `File:` title this element points at, from Wikidata first and OSM second.

    Written with spaces, because that is how the Commons API spells a title back to us and this
    string is the key the picture answer is looked up by — mappers, in turn, type `image` with
    underscores, exactly as a wiki page address does.
    """
    if entity and entity.image:
        return entity.image
    for key in ("image", "wikimedia_commons"):
        value = (tags.get(key) or "").strip()
        if value:
            return "File:" + value.split(":")[-1].replace("_", " ").strip()
    return None


def _picture(
    tags: dict[str, str], entity: Entity | None, pictures: dict[str, Picture]
) -> Picture | None:
    title = picture_title(tags, entity)
    return pictures.get(title) if title else None


def city_location(city: City) -> Location:
    return Location(lat=city.lat, lon=city.lon)


def element_location(element: Element) -> Location:
    return Location(lat=element.lat, lon=element.lon)
