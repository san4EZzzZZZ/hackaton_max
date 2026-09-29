"""`python -m ingest` — the operator's entry point.

The whole subsystem is reachable from here and from nothing else on a development machine, which is
the reason the CLI exists at all: a fetch that can only be started over HTTP cannot be inspected
before it is deployed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from core.config import configure_logging, get_settings
from ingest.cities import load_cities
from ingest.pipeline import ingest_cities
from server.catalog import EXTRA_DIR


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingest",
        description="Найти реальные места в городах через OpenStreetMap, Wikidata и Commons",
    )
    parser.add_argument(
        "--city",
        action="append",
        metavar="ГОРОД",
        help="город для обработки; повторите для нескольких",
    )
    parser.add_argument("--all", action="store_true", help="обойти все города из data/cities.json")
    parser.add_argument("--limit", type=int, default=None, help="максимум мест на город")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=f"куда писать; по умолчанию {EXTRA_DIR}",
    )
    parser.add_argument("--dry-run", action="store_true", help="посчитать и показать, ничего не записывать")
    parser.add_argument("--verbose", action="store_true", help="лог уровня DEBUG")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging("DEBUG" if args.verbose else settings.log_level)

    requested = list(args.city or [])
    if args.all:
        requested += load_cities(Path(settings.ingest_cities_file))
    if not requested:
        print("Нужен хотя бы один город: --city Казань или --all", file=sys.stderr)
        return 2

    reports = asyncio.run(
        ingest_cities(
            requested,
            settings=settings,
            out_dir=args.out_dir,
            total_limit=args.limit,
            dry_run=args.dry_run,
        )
    )
    for report in reports:
        print(report.summary())
        for failure in report.failures:
            print(f"  ! {failure}", file=sys.stderr)
    failed = [report for report in reports if not report.places]
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
