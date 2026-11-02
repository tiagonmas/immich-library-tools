#!/usr/bin/env python3
"""List Immich people and names that are highly similar to each one."""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from people_comma_finder import ImmichApiError, api_request, get_all_people


DEFAULT_THRESHOLD = 0.8
MERGE_PROMPT_THRESHOLD = 0.9


def format_similarity(score: float, *, color: bool) -> str:
    """Format a similarity percentage with a red-yellow-green terminal scale."""
    percentage = score * 100
    if not color:
        return f"{percentage:.1f}%"

    red = (255, 0, 0)
    yellow = (255, 255, 0)
    green = (0, 180, 0)
    if percentage <= 80:
        rgb = red
    elif percentage <= 90:
        amount = (percentage - 80) / 10
        rgb = tuple(
            round(start + (end - start) * amount)
            for start, end in zip(red, yellow)
        )
    else:
        amount = min((percentage - 90) / 10, 1)
        rgb = tuple(
            round(start + (end - start) * amount)
            for start, end in zip(yellow, green)
        )

    return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m{percentage:.1f}%\033[0m"


def merge_history_path() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    )
    return state_home / "immich-library-tools" / "people_similar_name_merges.json"


def load_merged_person_ids(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read merge history at {path}: {error}") from error

    if not isinstance(data, dict) or not isinstance(data.get("merged_person_ids"), list):
        raise RuntimeError(f"Merge history at {path} has an unexpected format")
    return {
        person_id
        for person_id in data["merged_person_ids"]
        if isinstance(person_id, str)
    }


def save_merged_person_ids(path: Path, person_ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps({"merged_person_ids": sorted(person_ids)}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


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


def high_confidence_pairs(
    results: list[tuple[dict, list[tuple[dict, float]]]],
) -> list[tuple[dict, dict, float]]:
    pairs: dict[tuple[str, str], tuple[dict, dict, float]] = {}
    for person, matches in results:
        person_id = person.get("id")
        if not isinstance(person_id, str):
            continue
        for other, score in matches:
            other_id = other.get("id")
            if score <= MERGE_PROMPT_THRESHOLD or not isinstance(other_id, str):
                continue
            if person_id == other_id:
                continue

            key = tuple(sorted((person_id, other_id)))
            if key in pairs:
                continue
            keeper, source = sorted(
                (person, other),
                key=lambda candidate: (
                    candidate["name"].casefold(),
                    str(candidate.get("id", "")),
                ),
            )
            pairs[key] = (keeper, source, score)

    return sorted(
        pairs.values(),
        key=lambda pair: (-pair[2], pair[0]["name"].casefold(), pair[1]["name"].casefold()),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List all Immich people and other names with high similarity scores."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="0..1",
        help="minimum name similarity score (default: 0.8, or 80%%)",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        metavar="PERCENT",
        help=(
            "show only people with at least one match at or above this percentage "
            "(0..100; for example, 90)"
        ),
    )
    parser.add_argument(
        "--only-matches",
        action="store_true",
        help="hide people who have no names at or above the similarity threshold",
    )
    args = parser.parse_args()
    if args.threshold is not None and not 0 <= args.threshold <= 1:
        parser.error("--threshold must be between 0 and 1")
    if args.min_similarity is not None and not 0 <= args.min_similarity <= 100:
        parser.error("--min-similarity must be between 0 and 100")
    return args


def main() -> int:
    args = parse_args()
    use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    history_path = merge_history_path()
    try:
        merged_person_ids = load_merged_person_ids(history_path)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

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

    people = [
        person
        for person in people
        if isinstance(person, dict)
        and isinstance(person.get("name"), str)
        and person["name"].strip()
    ]
    people.sort(key=lambda person: person["name"].casefold())

    min_score = (
        0.0
        if args.min_similarity is not None
        else (args.threshold if args.threshold is not None else DEFAULT_THRESHOLD)
    )
    if args.threshold is not None:
        min_score = max(min_score, args.threshold)
    if args.min_similarity is not None:
        min_score = max(min_score, args.min_similarity / 100)

    results = [
        (person, find_similar_people(person, people, min_score))
        for person in people
    ]
    if args.only_matches or args.min_similarity is not None:
        results = [(person, matches) for person, matches in results if matches]

    print(
        f"Found {len(people)} people. Showing {len(results)}. "
        f"Similarity threshold: {min_score:.0%}."
    )
    if (args.only_matches or args.min_similarity is not None) and not results:
        print("No people have similar names at this threshold.")

    for person, matches in results:
        print(f"\n{person['name']} (id: {person.get('id', 'unknown')})")
        if not matches:
            print("\tNo similar names found.")
            continue

        print("\tPossible duplicates:")
        for other, score in matches:
            similarity = format_similarity(score, color=use_color)
            print(
                f"\t- {other['name']} (id: {other.get('id', 'unknown')}; "
                f"similarity: {similarity})"
            )

    active_ids = {person.get("id") for person in people}
    for keeper, source, score in high_confidence_pairs(results):
        keeper_id = keeper["id"]
        source_id = source["id"]
        if keeper_id not in active_ids or source_id not in active_ids:
            continue
        previously_merged_ids = {keeper_id, source_id} & merged_person_ids
        if previously_merged_ids:
            merged_names = [
                person["name"]
                for person in (keeper, source)
                if person["id"] in previously_merged_ids
            ]
            print(
                f"\nSkipping merge prompt for {', '.join(merged_names)}; "
                "this script recorded a previous merge for this person."
            )
            continue

        similarity = format_similarity(score, color=use_color)
        print(
            f"\nHigh-confidence possible duplicate ({similarity} similarity):"
            f"\n\tKeep: {keeper['name']} (id: {keeper_id})"
            f"\n\tMerge into it: {source['name']} (id: {source_id})"
        )
        while True:
            try:
                answer = input("\tMerge these people? [y/N] ").strip().casefold()
            except EOFError:
                answer = "n"
            if answer in {"y", "yes"}:
                break
            if answer in {"", "n", "no"}:
                print("\tSkipped; moving to the next possible duplicate.")
                break
            print("\tPlease answer yes or no.")

        if answer not in {"y", "yes"}:
            continue

        try:
            merge_result = api_request(
                base_url,
                api_key,
                "/people/merge",
                {"ids": [keeper_id, source_id]},
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
                    if isinstance(result, dict) and result.get("id") == source_id
                ),
                None,
            )
        if source_result and source_result.get("success") is True:
            print(f"\tMerged {source['name']} into {keeper['name']}.")
            active_ids.remove(source_id)
            merged_person_ids.update({keeper_id, source_id})
            try:
                save_merged_person_ids(history_path, merged_person_ids)
            except OSError as error:
                print(
                    f"\tWarning: merge succeeded, but history could not be saved "
                    f"to {history_path}: {error}",
                    file=sys.stderr,
                )
        else:
            print(
                f"\tMerge was not confirmed as successful: {merge_result}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
