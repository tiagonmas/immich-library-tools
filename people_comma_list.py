#!/usr/bin/env python3
"""List Immich people whose names contain a comma."""

from __future__ import annotations

import os
import sys

from people_comma_finder import ImmichApiError, get_all_people


def main() -> int:
    base_url = os.environ.get("IMMICH_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not base_url or not api_key:
        print(
            "Set IMMICH_BASE_URL (for example http://localhost:2283) and IMMICH_API_KEY.",
            file=sys.stderr,
        )
        return 2

    if base_url.endswith("/api"):
        base_url = base_url[:-4].rstrip("/")

    try:
        people = get_all_people(base_url, api_key)
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    matching_people = [
        person
        for person in people
        if isinstance(person, dict)
        and isinstance(person.get("name"), str)
        and "," in person["name"]
    ]
    print(f"Found {len(matching_people)} people with a comma in their name.")
    for person in matching_people:
        print(f"{person['name']} (id: {person.get('id', 'unknown')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
