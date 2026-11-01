#!/usr/bin/env python3
"""List Immich people whose names contain a period."""

from __future__ import annotations

import os
import sys
import unicodedata
from difflib import SequenceMatcher

from people_comma_finder import ImmichApiError, api_request

SIMILARITY_THRESHOLD = 0.8


def normalized_name(name: str, *, remove_periods: bool = False) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    if remove_periods:
        without_accents = without_accents.replace(".", "")
    return " ".join(without_accents.split())


def find_similar_people(person: dict, people: list[dict]) -> list[tuple[dict, str, float]]:
    person_id = person.get("id")
    name = person["name"]
    normalized = normalized_name(name, remove_periods=True)
    matches: list[tuple[dict, str, float]] = []

    for other in people:
        if (
            not isinstance(other, dict)
            or other.get("id") == person_id
            or not isinstance(other.get("name"), str)
        ):
            continue

        other_name = other["name"]
        other_normalized = normalized_name(other_name, remove_periods=True)
        if normalized == other_normalized:
            matches.append((other, "exact match without periods", 1.0))
            continue

        score = SequenceMatcher(None, normalized, other_normalized).ratio()
        if score >= SIMILARITY_THRESHOLD:
            matches.append((other, "similar", score))

    return sorted(matches, key=lambda match: (-match[2], match[0]["name"].casefold()))


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
        response = api_request(base_url, api_key, "/people")
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if not isinstance(response, dict) or not isinstance(response.get("people"), list):
        print("Error: unexpected response from /people; expected a people list.", file=sys.stderr)
        return 1

    people = [
        person for person in response["people"] if isinstance(person, dict)
    ]
    matching_people = [
        person
        for person in people
        if isinstance(person.get("name"), str)
        and "." in person["name"]
    ]
    print(f"Found {len(matching_people)} people with a period in their name.")
    for person in matching_people:
        person_id = person.get("id")
        if not any(candidate.get("id") == person_id for candidate in people):
            continue

        print(f"\n{person['name']} (id: {person.get('id', 'unknown')})")
        similar_people = find_similar_people(person, people)
        if not similar_people:
            print("\tNo similar people found.")
            continue

        print("\tPotential merge options (ranked by similarity):")
        for index, (other, match_type, score) in enumerate(similar_people, start=1):
            if match_type == "exact match without periods":
                detail = match_type
            else:
                detail = f"{match_type}, similarity {score:.1%}"
            recommendation = " — recommended" if index == 1 else ""
            print(
                f"\t{index}. {other['name']} (id: {other.get('id', 'unknown')}; "
                f"{detail}){recommendation}"
            )

        recommended_person = similar_people[0][0]
        print(
            f"\tThis will merge {person['name']} into "
            f"{recommended_person['name']} and keep the recommended person's record."
        )
        try:
            answer = input("\tMerge with the recommended person? [y/N] ").strip().casefold()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("\tNo merge performed.")
            continue

        try:
            merge_result = api_request(
                base_url,
                api_key,
                "/people/merge",
                {"ids": [recommended_person["id"], person_id]},
            )
        except ImmichApiError as error:
            print(f"\tMerge failed: {error}", file=sys.stderr)
            continue

        source_result = None
        if isinstance(merge_result, list):
            source_result = next(
                (
                    result
                    for result in merge_result
                    if isinstance(result, dict) and result.get("id") == person_id
                ),
                None,
            )
        if source_result and source_result.get("success") is True:
            print(f"\tMerged {person['name']} into {recommended_person['name']}.")
            people = [candidate for candidate in people if candidate.get("id") != person_id]
        else:
            print(f"\tMerge was not confirmed as successful: {merge_result}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
