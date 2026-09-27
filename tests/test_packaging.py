"""What actually ships in the image, checked without building it.

The runtime image installs `requirements.txt` only and `tests/` is excluded from the build context, so
nothing inside the container can assert these. Both rules are pure text, which means CI can check them
here instead of leaving them to a daemon this project never runs locally: the seed files have to be in
the image, and the image must not carry dev tools or the developer's database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from server.catalog import DATA_FILE as PLACES_FILE
from server.guides import DATA_FILE as GUIDES_FILE

ROOT = Path(__file__).resolve().parent.parent

# The files the API reads at request time. A new seed loader belongs in this list together with its
# `!data/...` line in .dockerignore: forgetting the line is invisible at build time and answers 503 in
# production, which no test that runs outside the container would ever notice.
SEEDED_FILES = [PLACES_FILE, GUIDES_FILE]


def dockerignore_rules() -> list[str]:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("seeded", SEEDED_FILES, ids=lambda path: path.name)
def test_every_seed_file_is_excluded_by_the_wildcard_and_reincluded_by_name(
    seeded: Path,
) -> None:
    rules = dockerignore_rules()
    name = seeded.name
    assert "data/*" in rules, "локальная база данных не должна попадать в образ"
    assert f"!data/{name}" in rules, (
        f"data/{name} читается эндпоинтами, но в образ не включён — там будет 503"
    )
    # Docker applies the last matching rule, so the re-inclusion must come after the wildcard.
    assert rules.index(f"!data/{name}") > rules.index("data/*")
    assert seeded.is_file(), f"{seeded} отсутствует в репозитории"
    assert isinstance(json.loads(seeded.read_text(encoding="utf-8")), list)


def test_the_image_installs_runtime_dependencies_only() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    installs = re.findall(r"RUN python -m pip install[^\n]*", dockerfile)
    assert installs, "Dockerfile not found in the shape the test expects"
    assert all("dev-requirements" not in line for line in installs), (
        "pytest и PyYAML не должны попадать в runtime-образ"
    )
    assert any("requirements.txt" in line for line in installs)


def test_the_container_does_not_run_as_root() -> None:
    """The seed files are world-readable and the process faces the internet: an unprivileged user is
    the cheapest thing standing between a path-traversal bug and the host."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER bot$", dockerfile, re.MULTILINE)
    assert "useradd" in dockerfile


def test_the_dev_database_stays_out_of_the_image() -> None:
    rules = dockerignore_rules()
    for unwanted in ("*.db", ".env", "tests/"):
        assert unwanted in rules, f"{unwanted} должен быть исключён из сборки"
    assert ".env" in rules and "!.env.example" in rules, "пример настроек остаётся, токен — нет"
