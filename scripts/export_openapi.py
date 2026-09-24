#!/usr/bin/env python
"""Render DATA-API.yaml from the live application, or verify that it is current.

    python scripts/export_openapi.py            # regenerate the contract
    python scripts/export_openapi.py --check    # CI: fail when the file lags behind the code

The document is exactly what `create_app()` reports, so a route change can no longer drift away from its
documentation. `servers` is appended afterwards because it describes where this build runs rather than
something the code declares — the frontend reads its base URL from that block.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "DATA-API.yaml"

SERVERS = [{"url": "http://localhost:8080", "description": "Локальный сервер разработки"}]

# Importing the application pulls in core.config, which validates the environment on import. The spec
# needs no bot credentials and must never reach the MAX API.
os.environ.setdefault("BOT_TOKEN", "spec-export-placeholder-token")
os.environ.setdefault("AUTO_SETUP", "false")


def build_spec() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    from server.app import create_app

    schema = create_app().openapi()
    schema["servers"] = SERVERS
    return schema


def render() -> str:
    return yaml.safe_dump(build_spec(), allow_unicode=True, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the file on disk instead of writing it",
    )
    args = parser.parse_args(argv)

    text = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(
                f"{OUTPUT.name} is out of date — run: python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"{OUTPUT.name} matches the application")
        return 0

    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
