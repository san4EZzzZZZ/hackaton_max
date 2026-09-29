"""What counts as a place: the OpenStreetMap tags we ask for and where they land in the catalog.

Two rules shape this module.

The category vocabulary is closed and borrowed from the hand-written seed: a generated museum is
called «Музей» exactly like a curated one, because `GET /categories` and the Mini App's filters are
keyed on those strings. Adding a category is a product decision, not a mapping detail.

`tags` («зачем сюда идут») drive `GET /chips`, whose `place_count` is promised non-zero, and the test
suite holds the catalog to >90 % tagged objects — so every interest here carries at least one.

The same `Rule` set builds the Overpass query and classifies the answer, which is the point: a tag
added to one of the tuples is searched for and understood without a second edit anywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.schemas import TagId

MUSEUM = "Музей"
GALLERY = "Галерея"
THEATRE = "Театр"
PARK = "Парк"
MONUMENT = "Памятник"
ARCHITECTURE = "Архитектура"
CAFE = "Кафе"


@dataclass(frozen=True)
class Rule:
    """One OSM tag test, and the geometry types it applies to.

    `ways=False` marks tags that only ever sit on a node in practice (a café is mapped as a point
    even when the building around it is a way) — asking Overpass for the ways too costs a second
    pass over the city index for nothing.
    """

    key: str
    pattern: str = ".+"
    ways: bool = True

    def matches(self, tags: dict[str, str]) -> bool:
        value = tags.get(self.key)
        return value is not None and re.fullmatch(self.pattern, value, re.IGNORECASE) is not None

    def clause(self, scope: str) -> str:
        """One Overpass statement like `node["tourism"="museum"]["name"]`.

        `["name"]` is appended because an unnamed object cannot be shown to a visitor: dropping it
        server-side would waste most of the answer on elements with no label at all.
        """
        test = f'["{self.key}"~"^{self.pattern}$"]' if self.pattern != ".+" else f'["{self.key}"]'
        return f"{scope}{test}[\"name\"]"


@dataclass(frozen=True)
class Interest:
    """A catalog category and the OSM elements that belong to it."""

    category: str
    rules: tuple[Rule, ...]
    tags: tuple[TagId, ...]
    visit_duration_minutes: int
    #: Overpass answers a whole city at once; without a cap one Kazan fills the catalog with 400
    #: parks and the museums never surface. Selection ranks, then trims to `limit`.
    limit: int = 12

    @property
    def clauses(self) -> tuple[str, ...]:
        out = [rule.clause("node") for rule in self.rules]
        out += [rule.clause("way") for rule in self.rules if rule.ways]
        return tuple(out)


# Ordered: an element is claimed by the first interest that matches it, so the tag that says what
# people come *for* has to precede the tag that says what the bricks are.
INTERESTS: tuple[Interest, ...] = (
    Interest(
        category=MUSEUM,
        rules=(
            Rule("tourism", "museum"),
            Rule("tourism", "zoo"),
        ),
        tags=("culture", "photo"),
        visit_duration_minutes=90,
    ),
    Interest(
        category=THEATRE,
        rules=(
            Rule("amenity", "theatre"),
            Rule("amenity", "concert_hall"),
        ),
        tags=("culture",),
        visit_duration_minutes=120,
    ),
    Interest(
        category=GALLERY,
        rules=(
            Rule("tourism", "gallery"),
            Rule("tourism", "artwork"),
        ),
        tags=("culture", "photo"),
        visit_duration_minutes=60,
    ),
    Interest(
        category=PARK,
        rules=(
            Rule("leisure", "park"),
            Rule("leisure", "garden"),
            Rule("leisure", "nature_reserve"),
            Rule("tourism", "viewpoint"),
        ),
        tags=("walk", "photo"),
        visit_duration_minutes=60,
        limit=8,
    ),
    Interest(
        category=MONUMENT,
        rules=(
            Rule("historic", "monument"),
            Rule("historic", "memorial"),
            Rule("historic", "obelisk"),
            Rule("historic", "boundary_stone"),
        ),
        tags=("photo", "walk"),
        visit_duration_minutes=20,
    ),
    Interest(
        category=ARCHITECTURE,
        rules=(
            Rule(
                "historic",
                "castle|fortification|city_gate|tower|building|church|chapel|synagogue|mosque|"
                "archaeological_site|manor|city_wall|windmill|water_tower",
            ),
            Rule("tourism", "attraction"),
            Rule("amenity", "place_of_worship"),
        ),
        tags=("photo", "walk"),
        visit_duration_minutes=30,
    ),
    Interest(
        # fast_food is deliberately absent: a chain burger on every corner is not where a walking
        # route should bring a visitor, and it would drown the independent places this category is
        # about. `brand` repeats are capped separately in normalize.
        category=CAFE,
        rules=(
            Rule("amenity", "cafe|coffee_shop|confectionery|pastry", ways=False),
            Rule("amenity", "restaurant|bakery|ice_cream", ways=False),
        ),
        tags=("coffee", "food"),
        visit_duration_minutes=45,
    ),
)

BY_CATEGORY: dict[str, Interest] = {interest.category: interest for interest in INTERESTS}

CATEGORIES: tuple[str, ...] = tuple(interest.category for interest in INTERESTS)


def category_of(tags: dict[str, str]) -> str | None:
    """The catalog category for raw OSM tags, or None when the element is not a place."""
    for interest in INTERESTS:
        if any(rule.matches(tags) for rule in interest.rules):
            return interest.category
    return None


def tags_of(category: str) -> tuple[TagId, ...]:
    interest = BY_CATEGORY.get(category)
    return interest.tags if interest else ()


def visit_minutes(category: str) -> int:
    interest = BY_CATEGORY.get(category)
    return interest.visit_duration_minutes if interest else 60


def is_notable(tags: dict[str, str]) -> bool:
    """Does something besides the tag itself argue this object is worth a visitor's time?

    An OSM element with a `wikipedia`/`wikidata` link, a picture or an attraction tag has been
    written about somewhere; a place_of_worship with none of them is a chapel in a yard.
    """
    if tags.get("tourism") in {"attraction", "museum", "gallery", "artwork", "viewpoint", "zoo"}:
        return True
    return any(tags.get(key) for key in ("wikidata", "wikipedia", "image", "wikimedia_commons"))


def worth_showing(category: str, tags: dict[str, str]) -> bool:
    """Drop the elements a category admits but a catalog should not.

    `amenity=place_of_worship` is the only such case: it is genuinely architecture, and it also
    returns every working parish church on a residential street. Where the object is listed as an
    attraction or carries a wiki link it belongs in the catalog; with neither, it is a building
    rather than a destination.
    """
    if category == ARCHITECTURE and tags.get("amenity") == "place_of_worship":
        return is_notable(tags)
    return True
