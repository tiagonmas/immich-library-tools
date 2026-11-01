#!/usr/bin/env python3
"""List Immich people and names that are highly similar to each one."""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from difflib import SequenceMatcher

from people_comma_finder import ImmichApiError, api_request


DEFAULT_THRESHOLD = 0.8


def normalized_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in without_accents
    )
    return " ".join(without_punctuation.split())


def find_similar_people(
    person: dict, people: list[dict], threshold: float
) -> list[tuple[dict, float]]:
    person_id = person.get("id")
    name = person["name"]
    normalized = normalized_name(name)
    matches: list[tuple[dict, float]] = []

    for other in people:
        if person_id is not None and other.get("id") == person_id:
            continue
        other_name = other.get("name")
        if not isinstance(other_name, str) or not other_name.strip():
            continue

        other_normalized = normalized_name(other_name)
        score = (
            SequenceMatcher(None, normalized, other_normalized).ratio()
            if normalized and other_normalized
            else 0.0
        )
        if score >= threshold:
            matches.append((other, score))

    return sorted(matches, key=lambda match: (-match[1], match[0]["name"].casefold()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List all Immich people and other names with high similarity scores."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="0..1",
        help="minimum name similarity score (default: 0.8, or 80%%)",
    )
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1:
        parser.error("--threshold must be between 0 and 1")
    return args


def main() -> int:
    args = parse_args()
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
        response = api_request(base_url, api_key, "/people")
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if not isinstance(response, dict) or not isinstance(response.get("people"), list):
        print("Error: unexpected response from /people; expected a people list.", file=sys.stderr)
        return 1

    people = [
        person
        for person in response["people"]
        if isinstance(person, dict)
        and isinstance(person.get("name"), str)
        and person["name"].strip()
    ]
    people.sort(key=lambda person: person["name"].casefold())

    print(f"Found {len(people)} people. Similarity threshold: {args.threshold:.0%}.")
    for person in people:
        print(f"\n{person['name']} (id: {person.get('id', 'unknown')})")
        matches = find_similar_people(person, people, args.threshold)
        if not matches:
            print("\tNo similar names found.")
            continue

        print("\tPossible duplicates:")
        for other, score in matches:
            print(
                f"\t- {other['name']} (id: {other.get('id', 'unknown')}; "
                f"similarity: {score:.1%})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
