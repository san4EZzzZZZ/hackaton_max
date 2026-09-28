"""HTTP contract of the catalog endpoints, as the Mini App sees it.

Two shapes are load-bearing for the frontend and easy to break by accident: `/places` answers a bare
JSON array (not a `{items, total}` envelope), and an empty selection is `200 []` rather than an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import app_client

import server.catalog as catalog_module

CITY = "Ростов-на-Дону"
PLACES = "/api/v1/places"
CATEGORIES = "/api/v1/categories"
CITIES = "/api/v1/cities"


def ids(response) -> list[str]:
    return [item["id"] for item in response.json()]


def test_places_answers_a_bare_array_of_every_known_object(client: TestClient, places: list[dict]) -> None:
    response = client.get(PLACES)
    assert response.status_code == 200
    assert isinstance(response.json(), list), "the envelope shape is part of the published contract"
    assert len(response.json()) == len(places) >= 1, "the fixture and the endpoint must read the same file"
    assert {"id", "title", "category", "tags", "location", "city", "price"} <= set(response.json()[0])


def test_place_filters_narrow_the_same_collection(client: TestClient, places: list[dict]) -> None:
    museums = [item for item in places if item["category"] == "Музей"]
    assert museums, "the seed data must keep an object in this category"
    assert len(client.get(PLACES, params={"category": "Музей"}).json()) == len(museums)
    assert len(client.get(PLACES, params={"category": "музей"}).json()) == len(museums)
    assert len(client.get(PLACES, params={"city": "Ростов"}).json()) == len(places)
    pushkin = [item for item in places if item["is_pushkin_card"]]
    assert len(client.get(PLACES, params={"is_pushkin_card": True}).json()) == len(pushkin) == 5
    free = [item for item in places if item["price"] == 0]
    assert len(client.get(PLACES, params={"max_price": 0}).json()) == len(free)


def test_filters_combine(client: TestClient) -> None:
    response = client.get(
        PLACES, params={"city": "ростов-на-Дону", "category": "Театр", "is_pushkin_card": True}
    )
    assert response.status_code == 200
    for item in response.json():
        assert item["city"] == CITY and item["category"] == "Театр" and item["is_pushkin_card"]


def test_an_empty_selection_is_not_an_error(client: TestClient) -> None:
    response = client.get(PLACES, params={"city": "Москва"})
    assert response.status_code == 200
    assert response.json() == []
    assert client.get(PLACES, params={"max_price": 0.01, "is_pushkin_card": False}).status_code == 200


def test_an_impossible_price_is_rejected_before_it_is_evaluated(client: TestClient) -> None:
    response = client.get(PLACES, params={"max_price": -1})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "422 keeps FastAPI's error list"


def test_a_known_object_comes_back_whole(client: TestClient) -> None:
    listed = client.get(PLACES).json()[0]
    detail = client.get(f"{PLACES}/{listed['id']}")
    assert detail.status_code == 200
    assert detail.json() == listed


def test_an_unknown_object_is_a_404_with_a_readable_detail(client: TestClient) -> None:
    response = client.get(f"{PLACES}/no-such-place")
    assert response.status_code == 404
    # The wording belongs to the endpoint, not to the contract; only its shape is asserted here.
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.strip()


def test_categories_are_the_ones_the_data_actually_has(client: TestClient, places: list[dict]) -> None:
    response = client.get(CATEGORIES)
    assert response.status_code == 200
    assert response.json() == list(dict.fromkeys(item["category"] for item in places))


def test_tags_on_a_place_are_ids_the_chip_screen_can_ask_for(
    client: TestClient, places: list[dict]
) -> None:
    """`Place.tags` and `Chip.id` are one vocabulary, or the filter the screen sends matches nothing.

    The counts come from `/chips` rather than from a literal list in here: adding a chip without
    teaching the data to use it should fail at the chip, not at this test.
    """
    chips = client.get("/api/v1/chips").json()
    declared = {chip["id"] for chip in chips}
    tagged = [item for item in places if item["tags"]]
    assert len(tagged) >= 0.9 * len(places), "разметка есть у 90 % мест — иначе чипы декорация"
    for item in tagged:
        assert set(item["tags"]) <= declared, item["id"]
    for chip in chips:
        counted = sum(1 for item in places if set(item["tags"]) & set(chip["tags"]))
        assert counted == chip["place_count"], chip["id"]


def test_the_number_a_chip_promises_is_the_list_places_sells(
    client: TestClient, places: list[dict]
) -> None:
    """`?tag=` is the same axis `/chips` counts and the planner filters — one predicate, three screens.

    The chip screen says «☕ 5», the list behind it has to hold five cards, and the walk it generates
    has to visit those five. The assertion reads its numbers out of `/chips` so a new chip cannot be
    added as a promise nobody kept.
    """
    for chip in client.get("/api/v1/chips").json():
        response = client.get(PLACES, params={"tag": chip["id"]})
        assert response.status_code == 200, chip["id"]
        found = response.json()
        assert len(found) == chip["place_count"], chip["id"]
        assert response.headers["X-Total-Count"] == str(chip["place_count"]), chip["id"]
        for item in found:
            assert set(item["tags"]) & set(chip["tags"]), (chip["id"], item["id"])
        expected = [
            item["id"]
            for item in places
            if set(item["tags"]) & set(chip["tags"])
        ]
        assert [item["id"] for item in found] == expected, "the tag filter keeps catalog order"


def test_several_tags_in_one_url_widen_the_way_the_chip_screen_reads_them(
    client: TestClient, places: list[dict]
) -> None:
    """Several tags are ИЛИ: a visitor who wants coffee or a bite gets both lists, not their gap."""
    wanted = ["coffee", "food"]
    joined = client.get(PLACES, params={"tag": wanted})
    assert joined.status_code == 200
    single = [set(ids(client.get(PLACES, params={"tag": tag}))) for tag in wanted]
    assert set(ids(joined)) == single[0] | single[1], "ИЛИ, а не пересечение"
    assert len(joined.json()) >= max(len(s) for s in single)
    for item in joined.json():
        assert set(item["tags"]) & set(wanted), item["id"]


def test_tag_and_category_narrow_together_instead_of_one_replacing_the_other(
    client: TestClient,
) -> None:
    """Театр — это «что это», фото — «зачем туда идут»: пересечение обязано быть уже каждой стороны.

    Памятник без снимка из этой выборки выпадает, а не перетягивает фильтр на себя: на категории,
    где все места помечены одним тегом, сузить было бы нечем, и тест молча проверял бы подмену.
    """
    photo = set(ids(client.get(PLACES, params={"tag": "photo"})))
    monuments = set(ids(client.get(PLACES, params={"category": "Памятник"})))
    both = client.get(PLACES, params={"tag": "photo", "category": "Памятник"})
    assert both.status_code == 200
    combined = set(ids(both))
    assert combined == photo & monuments, "две оси не должны превращаться в одну"
    assert combined < photo and combined < monuments, "фильтр обязан сужать, а не переключаться"
    for item in both.json():
        assert item["category"] == "Памятник" and "photo" in item["tags"], item["id"]

    # A combination with nothing under it is an empty collection, not a broken request — the walk
    # screen answers 404 for the same dead end because there the route itself would not exist.
    empty = client.get(PLACES, params={"tag": "coffee", "category": "Парк"})
    assert empty.status_code == 200 and empty.json() == []


def test_a_tag_typo_is_an_empty_shelf_rather_than_a_rejected_request(client: TestClient) -> None:
    """Same behavior as `?category=nope`: a filter is a filter, whatever its vocabulary."""
    unknown = client.get(PLACES, params={"tag": "no-such-mood"})
    assert unknown.status_code == 200 and unknown.json() == []
    assert (
        client.get(PLACES, params={"category": "no-such-mood"}).json() == []
    ), "the two axes must not differ in what a miss costs the caller"


def test_a_tag_is_compared_the_way_the_chip_id_is_spelled(client: TestClient) -> None:
    """Case and stray spaces are the caller's typographic problem, not a miss."""
    full = client.get(PLACES, params={"tag": "culture"}).json()
    assert full
    assert client.get(PLACES, params={"tag": "CULTURE"}).json() == full
    assert client.get(PLACES, params={"tag": "  culture  "}).json() == full

    # An empty value drops out of the filter instead of selecting nothing: `?tag=` means «no mood
    # chosen yet», and a screen that sends it must still see the catalog.
    assert len(client.get(PLACES, params={"tag": " "}).json()) == len(client.get(PLACES).json())


def test_a_tag_filter_leaves_pagination_and_radius_alone(client: TestClient) -> None:
    """`limit` cuts the tail, the header still reports the whole shelf."""
    total = client.get(PLACES, params={"tag": "photo"})
    assert total.status_code == 200
    page = client.get(PLACES, params={"tag": "photo", "limit": 3})
    assert len(page.json()) == 3
    assert page.headers["X-Total-Count"] == total.headers["X-Total-Count"]
    assert page.json() == total.json()[:3]

    # The center of the catalog's own cluster keeps the tag axis alive: «Перекусить» near Theatre
    # Square has to answer with the cafés, not with a radius that quietly dropped them all.
    food = set(ids(client.get(PLACES, params={"tag": "food"})))
    center = client.get(f"{PLACES}/theatre-square").json()["location"]
    nearby = client.get(
        PLACES,
        params={"tag": "food", "near_lat": center["lat"], "near_lon": center["lon"], "radius_km": 5},
    )
    assert nearby.status_code == 200
    assert ids(nearby), "еда в шаговой доступности от площади — смысл чипа «Перекусить»"
    assert set(ids(nearby)) <= food, "радиус может только урезать выборку по тегу"


@pytest.mark.parametrize(
    "path",
    [
        PLACES,
        f"{PLACES}/theatre-square",
        CATEGORIES,
        CITIES,
        "/api/v1/chips",
        "/api/v1/routes/generate",
        # Both guide screens read the catalog too: the index pairs a guide with its place, and the
        # guide answer echoes price and opening hours out of it.
        "/api/v1/guides",
        "/api/v1/places/theatre-square/guide",
    ],
)
def test_an_unreadable_catalog_reports_503_rather_than_500(tmp_path: Path, monkeypatch, path: str) -> None:
    monkeypatch.setattr(catalog_module, "DATA_FILE", tmp_path / "absent.json")
    with app_client() as client:
        if path.endswith("generate"):
            response = client.post(path, json={"city": CITY})
        else:
            response = client.get(path)
        assert response.status_code == 503, path
        assert "detail" in response.json(), path


def test_a_search_word_reaches_the_text_a_visitor_reads(client: TestClient) -> None:
    # "кувшином" is in no title, no category and no city — it only exists inside the description of
    # Театральная площадь, which is what makes this a test of description search rather than of names.
    found = client.get(PLACES, params={"q": "кувшином"})
    assert found.status_code == 200
    assert [item["id"] for item in found.json()] == ["theatre-square"]
    assert found.headers["X-Total-Count"] == "1"

    # «фонтан» stopped being unique when the catalog gained the fountain on the same square (#36): the
    # word matches the description of one record and the title of the other, in file order.
    assert [item["id"] for item in client.get(PLACES, params={"q": "фонтан"}).json()] == [
        "theatre-square",
        "atlanty-fountain",
    ]


def test_search_words_narrow_instead_of_broadening(client: TestClient) -> None:
    broad = {item["id"] for item in client.get(PLACES, params={"q": "ростов"}).json()}
    narrow = {item["id"] for item in client.get(PLACES, params={"q": "ростов девушка"}).json()}
    assert broad > narrow, "adding a word can only cut the result set"
    assert client.get(PLACES, params={"q": "ростов несуществующее слово"}).json() == []


def test_search_combines_with_the_other_filters(client: TestClient) -> None:
    items = client.get(
        PLACES, params={"city": "Ростов", "q": "музей", "min_rating": 4.5}
    ).json()
    assert items
    for item in items:
        assert item["rating"] >= 4.5
        assert "музей" in (item["title"] + (item["description"] or "") + item["category"]).lower()


def test_a_query_shorter_than_two_characters_is_refused_by_validation(client: TestClient) -> None:
    response = client.get(PLACES, params={"q": "а"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list), "this one is FastAPI's own error list"


def test_min_rating_keeps_objects_at_or_above_the_line(client: TestClient, places: list[dict]) -> None:
    threshold = 4.6
    expected = [item["id"] for item in places if item["rating"] >= threshold]
    response = client.get(PLACES, params={"min_rating": threshold})
    assert [item["id"] for item in response.json()] == expected
    assert response.headers["X-Total-Count"] == str(len(expected))


def test_min_rating_out_of_range_is_refused(client: TestClient) -> None:
    assert client.get(PLACES, params={"min_rating": 5.5}).status_code == 422
    assert client.get(PLACES, params={"min_rating": -1}).status_code == 422


def test_radius_selects_by_straight_line_distance(client: TestClient, places: list[dict]) -> None:
    from server.routing import haversine_km
    from server.schemas import Location

    center = Location(**places[0]["location"])
    widths = []
    for radius in (1, 3, 10):
        response = client.get(
            PLACES,
            params={"near_lat": center.lat, "near_lon": center.lon, "radius_km": radius},
        )
        assert response.status_code == 200, radius
        expected = {
            item["id"]
            for item in places
            if haversine_km(center, Location(**item["location"])) <= radius
        }
        assert {item["id"] for item in response.json()} == expected, radius
        assert places[0]["id"] in {item["id"] for item in response.json()}, "the center is at 0 km"
        widths.append(len(response.json()))
    assert widths == sorted(widths) and widths[0] < widths[-1]


def test_radius_needs_a_complete_center(client: TestClient, places: list[dict]) -> None:
    center = places[0]["location"]
    # These two 422s carry a plain message, unlike validation errors: the mistake is a combination of
    # individually valid parameters, which the request model cannot express.
    only_radius = client.get(PLACES, params={"radius_km": 5})
    assert only_radius.status_code == 422
    assert isinstance(only_radius.json()["detail"], str)
    assert "near_lat" in only_radius.json()["detail"]

    half_center = client.get(PLACES, params={"near_lat": center["lat"]})
    assert half_center.status_code == 422
    assert isinstance(half_center.json()["detail"], str)

    assert client.get(
        PLACES, params={"near_lat": center["lat"], "near_lon": center["lon"]}
    ).status_code == 200, "a center without a radius just orders nothing away"


def test_a_page_is_a_slice_of_the_unpaginated_collection(client: TestClient, places: list[dict]) -> None:
    whole = [item["id"] for item in client.get(PLACES).json()]
    assert whole == [item["id"] for item in places]

    response = client.get(PLACES, params={"limit": 4, "offset": 6})
    assert [item["id"] for item in response.json()] == whole[6:10]
    assert response.headers["X-Offset"] == "6"

    first_page = client.get(PLACES, params={"limit": 2})
    assert [item["id"] for item in first_page.json()] == whole[:2]
    assert len(first_page.json()) == 2 < len(whole)


def test_the_total_header_ignores_the_page_size(client: TestClient, places: list[dict]) -> None:
    totals = {
        client.get(PLACES, params=params).headers["X-Total-Count"]
        for params in (
            {},
            {"limit": 1},
            {"limit": 1, "offset": 1},
            {"offset": len(places)},
            {"city": "Ростов"},
        )
    }
    assert totals == {str(len(places))}
    museums = client.get(PLACES, params={"category": "Музей", "limit": 1})
    assert int(museums.headers["X-Total-Count"]) == len(
        [item for item in places if item["category"] == "Музей"]
    )
    assert len(museums.json()) == 1


def test_an_offset_past_the_end_answers_an_empty_page(client: TestClient, places: list[dict]) -> None:
    response = client.get(PLACES, params={"offset": len(places) + 50})
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == str(len(places))


def test_pages_have_boundaries_a_client_can_rely_on(client: TestClient) -> None:
    assert client.get(PLACES, params={"limit": 0}).status_code == 422
    assert client.get(PLACES, params={"limit": 201}).status_code == 422
    assert client.get(PLACES, params={"offset": -1}).status_code == 422
    assert client.get(PLACES, params={"radius_km": 0}).status_code == 422


def test_cities_offers_only_what_the_catalog_can_show(client: TestClient, places: list[dict]) -> None:
    response = client.get(CITIES)
    assert response.status_code == 200
    summary = response.json()
    assert isinstance(summary, list) and summary
    assert {item["city"] for item in summary} == {place["city"] for place in places}
    assert sum(item["place_count"] for item in summary) == len(places)
    for item in summary:
        own = [place for place in places if place["city"] == item["city"]]
        assert item["categories"] == list(dict.fromkeys(place["category"] for place in own))
    assert {"city", "place_count", "categories"} == set(summary[0])


def test_the_new_query_parameters_and_headers_are_documented(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    parameters = spec["paths"][PLACES]["get"]["parameters"]
    place_params = {parameter["name"] for parameter in parameters}
    assert {"q", "min_rating", "near_lat", "near_lon", "radius_km", "limit", "offset"} <= place_params
    tag = next(parameter for parameter in parameters if parameter["name"] == "tag")
    assert tag["schema"]["anyOf"][0]["type"] == "array", "the filter repeats in the URL"
    assert "chips" in tag["description"], "the description has to name where the ids come from"
    headers = spec["paths"][PLACES]["get"]["responses"]["200"]["headers"]
    assert set(headers) == {"X-Total-Count", "X-Offset"}
    responses = spec["paths"][PLACES]["get"]["responses"]
    assert {"200", "422", "503"} <= set(responses)
    # 422 on this endpoint really has two shapes, and the contract has to admit both.
    declared = responses["422"]["content"]["application/json"]["schema"]["oneOf"]
    assert {"$ref": "#/components/schemas/HTTPValidationError"} in declared
    assert {"$ref": "#/components/schemas/ApiError"} in declared
    assert CITIES in spec["paths"], "the frontend reads the contract, not the code"
