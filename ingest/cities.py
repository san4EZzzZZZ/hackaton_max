"""The list of cities `python -m ingest --all` walks.

Kept in a data file rather than in code so that adding a city to the demo is an edit to
`data/cities.json`, not a pull request — and so a run can be narrowed from the command line without
deleting anything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ingest.transport import IngestSourceError

logger = logging.getLogger(__name__)


def load_cities(path: Path) -> list[str]:
    """City names, in file order, blanks and duplicates removed.

    A non-string entry is dropped rather than fatal: this file is edited by whoever wants another
    city on the map, and a typo in it should shrink the run, not break the deploy.
    """
    if not path.is_file():
        raise IngestSourceError(f"City list not found at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IngestSourceError(f"Cannot read {path}: {error}") from error
    if not isinstance(raw, list):
        raise IngestSourceError(f"Invalid city list {path.name}: a list is expected")

    names: list[str] = []
    for entry in raw:
        name = entry.strip() if isinstance(entry, str) else ""
        if name and name not in names:
            names.append(name)
    if not names:
        raise IngestSourceError(f"{path.name} lists no enabled city")
    return names
