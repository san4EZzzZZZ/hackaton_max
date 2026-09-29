"""Where a generated city lives: one JSON file per city under `data/places.d/`.

The seed at `data/places.json` is a hand-checked document about one city and stays that way; a
generated city is a different kind of object, and giving each its own file is what keeps them
distinguishable. The API serves the two indistinguishably, `git` never sees a fetch rewrite a
curated record, and re-running a city replaces exactly one file.

The whole file is rewritten per run rather than merged. A partial merge would be a lie by omission:
an object OpenStreetMap deleted last week would stay in the catalog forever, and nothing in the
record would say it was never re-checked. Re-running a city is the review, and `--dry-run` shows the
diff before it lands.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ingest.normalize import slugify

logger = logging.getLogger(__name__)


def city_path(directory: Path, city_name: str) -> Path:
    return directory / f"{slugify(city_name)}.json"


def read_city(path: Path) -> list[dict[str, Any]]:
    """The records in a city file, or [] when it does not exist yet."""
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return list(raw) if isinstance(raw, list) else []


def write_city(directory: Path, city_name: str, records: list[dict[str, Any]]) -> Path:
    """Replace the city's file with exactly these records.

    Written to a sibling temporary file and renamed, because the API reads this directory while it
    serves requests: a half-written file would surface as a 503 on `/places` for whoever happens to
    ask during a fetch.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = city_path(directory, city_name)
    payload = json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(f".json.tmp-{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    logger.info("Wrote %d places to %s", len(records), path)
    return path
