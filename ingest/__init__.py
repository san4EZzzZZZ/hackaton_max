"""Automatic catalog discovery: from a city name to a file of real places under `data/places.d/`.

The hand-written `data/places.json` stays what it is — one curated city, every field checked. This
package answers the next question, which is what a visitor in Kazan or Kaliningrad sees: it asks
OpenStreetMap what is there, Wikidata what to say about it and Wikimedia Commons what to show, and
writes the same `Place` records the API already serves.

Nothing here is a server. `ingest.pipeline.ingest_city` is the whole operation, `python -m ingest`
is how a person runs it, and `POST /api/v1/admin/ingest` is how the deployed box runs it — all three
share the code, and none of them can be reached without naming a city.
"""

from __future__ import annotations

__all__: list[str] = []
