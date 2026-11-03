#!/usr/bin/env python3
"""Find similar Immich tags and interactively merge high-confidence matches."""

from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote

from people_comma_finder import ImmichApiError, api_request
from tags_comma_list import PAGE_SIZE, get_all_tags, get_asset_page, tag_value


DEFAULT_THRESHOLD = 0.8
REPLACEMENT_PROMPT_THRESHOLD = 0.9
WRITE_BATCH_SIZE = 100


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


def similarity(left: str, right: str, threshold: float) -> float:
    """Compare names while avoiding SequenceMatcher work for impossible matches."""
    if not left or not right:
        return 0.0
    length_upper_bound = 2 * min(len(left), len(right)) / (len(left) + len(right))
    if length_upper_bound < threshold:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def period_only_pair(left_name: str, right_name: str) -> tuple[str, str] | None:
    """Return dotted source and undotted destination for exact period variants."""
    left_has_period = "." in left_name
    right_has_period = "." in right_name
    if left_has_period == right_has_period:
        return None

    dotted, undotted = (
        (left_name, right_name) if left_has_period else (right_name, left_name)
    )
    dotted_as_words = " ".join(dotted.replace(".", " ").split())
    undotted_words = " ".join(undotted.split())
    if dotted_as_words == undotted_words:
        return dotted, undotted
    return None


def capitalization_only_pair(
    left_name: str, right_name: str
) -> tuple[str, str] | None:
    """Return lowercase source and initial-capital destination for case-only pairs."""
    if not left_name or not right_name or left_name[1:] != right_name[1:]:
        return None
    if left_name[0].islower() and left_name[0].upper() == right_name[0]:
        return left_name, right_name
    if right_name[0].islower() and right_name[0].upper() == left_name[0]:
        return right_name, left_name
    return None


def find_similar_tags(
    tags: list[dict], threshold: float
) -> list[tuple[dict, list[tuple[dict, float]]]]:
    named_tags = [
        tag
        for tag in tags
        if isinstance(tag_value(tag), str) and tag_value(tag).strip()
    ]
    named_tags.sort(key=lambda tag: (tag_value(tag).casefold(), str(tag.get("id", ""))))
    normalized = [normalized_name(tag_value(tag)) for tag in named_tags]
    matches: list[list[tuple[dict, float]]] = [[] for _tag in named_tags]

    for left_index, left_name in enumerate(normalized):
        for right_index in range(left_index + 1, len(named_tags)):
            score = similarity(left_name, normalized[right_index], threshold)
            if score >= threshold:
                matches[left_index].append((named_tags[right_index], score))
                matches[right_index].append((named_tags[left_index], score))

    results = []
    for tag, tag_matches in zip(named_tags, matches):
        tag_matches.sort(key=lambda match: (-match[1], tag_value(match[0]).casefold()))
        results.append((tag, tag_matches))
    return results


def get_tag_assets(base_url: str, api_key: str, tag_id: str) -> list[dict]:
    """Fetch every image and video associated with a tag, following cursors."""
    assets: list[dict] = []
    known_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    while True:
        body: dict[str, object] = {
            "filter": {"tagIds": {"any": [tag_id]}},
            "size": PAGE_SIZE,
        }
        if cursor:
            if cursor in seen_cursors:
                raise ImmichApiError(
                    f"Immich asset search repeated a cursor for tag {tag_id}"
                )
            seen_cursors.add(cursor)
            body["cursor"] = cursor

        response = api_request(base_url, api_key, "/search/metadata", body)
        items, _next_page, next_cursor = get_asset_page(response)
        for asset in items:
            asset_id = asset.get("id")
            if not isinstance(asset_id, str):
                raise ImmichApiError(
                    "Immich asset search returned an asset without an ID"
                )
            if asset_id not in known_ids:
                assets.append(asset)
                known_ids.add(asset_id)

        if not next_cursor:
            return assets
        cursor = str(next_cursor)
        time.sleep(0.05)


def merge_tag_into(
    base_url: str,
    api_key: str,
    source_tag: dict,
    destination_tag: dict,
    source_assets: list[dict],
) -> None:
    """Assign the destination tag to source assets, then delete the source tag."""
    source_id = source_tag.get("id")
    destination_id = destination_tag.get("id")
    if not isinstance(source_id, str) or not isinstance(destination_id, str):
        raise ImmichApiError("Cannot merge tags without valid tag IDs")

    asset_ids = [asset["id"] for asset in source_assets]
    for start in range(0, len(asset_ids), WRITE_BATCH_SIZE):
        api_request(
            base_url,
            api_key,
            "/tags/assets",
            {
                "tagIds": [destination_id],
                "assetIds": asset_ids[start : start + WRITE_BATCH_SIZE],
            },
            method="PUT",
        )

    api_request(
        base_url,
        api_key,
        f"/tags/{quote(source_id, safe='')}",
        method="DELETE",
    )


def prompt_for_replacement(
    base_url: str,
    api_key: str,
    left: dict,
    right: dict,
    score: float,
    asset_cache: dict[str, list[dict]],
) -> set[str] | None:
    left_id = left.get("id")
    right_id = right.get("id")
    if not isinstance(left_id, str) or not isinstance(right_id, str):
        print("\tCannot offer replacement options: one or both tags have no ID.")
        return None

    try:
        if left_id not in asset_cache:
            asset_cache[left_id] = get_tag_assets(base_url, api_key, left_id)
        if right_id not in asset_cache:
            asset_cache[right_id] = get_tag_assets(base_url, api_key, right_id)
    except ImmichApiError as error:
        print(f"\tCould not load assets for replacement options: {error}")
        return None

    left_name = tag_value(left)
    right_name = tag_value(right)
    print(f"\nHigh similarity ({score * 100:.1f}%): {left_name} ↔ {right_name}")
    left_asset_count = len(asset_cache[left_id])
    right_asset_count = len(asset_cache[right_id])

    unused_tags = [
        (tag, tag_id)
        for tag, tag_id, asset_count in (
            (left, left_id, left_asset_count),
            (right, right_id, right_asset_count),
        )
        if asset_count == 0
    ]
    if unused_tags:
        deleted_ids: set[str] = set()
        for unused_tag, unused_tag_id in unused_tags:
            print(
                f"\tTag {tag_value(unused_tag)!r} has zero assets; "
                "deleting it automatically."
            )
            try:
                api_request(
                    base_url,
                    api_key,
                    f"/tags/{quote(unused_tag_id, safe='')}",
                    method="DELETE",
                )
            except ImmichApiError as error:
                print(f"\tCould not delete unused tag: {error}")
                return deleted_ids or None
            deleted_ids.add(unused_tag_id)
            print(f"\tDeleted unused tag {tag_value(unused_tag)!r}.")
        return deleted_ids

    left_has_period = "." in left_name
    right_has_period = "." in right_name
    exact_period_pair = period_only_pair(left_name, right_name)
    capitalization_pair = capitalization_only_pair(left_name, right_name)

    if exact_period_pair:
        source_tag, destination_tag = (
            (left, right) if left_has_period else (right, left)
        )
        print(
            f"\tExact period variant; automatically merging "
            f"{tag_value(source_tag)!r} into {tag_value(destination_tag)!r}."
        )
    elif capitalization_pair:
        source_name, destination_name = capitalization_pair
        source_tag, destination_tag = (
            (left, right)
            if tag_value(left) == source_name
            else (right, left)
        )
        print(
            f"\tCapitalization-only variant; automatically merging "
            f"{source_name!r} into {destination_name!r}."
        )
    elif left_has_period != right_has_period:
        source_tag, destination_tag = (
            (left, right) if left_has_period else (right, left)
        )
        source_id = source_tag["id"]
        destination_id = destination_tag["id"]
        print(
            f"\tDotted tag {tag_value(source_tag)!r}: "
            f"{len(asset_cache[source_id])} assets."
        )
        print(
            f"\tUndotted tag {tag_value(destination_tag)!r}: "
            f"{len(asset_cache[destination_id])} assets."
        )
        try:
            answer = input(
                f"\tMerge the dotted tag into {tag_value(destination_tag)!r}, "
                "removing the period? [y/N] "
            ).strip().casefold()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("\tSkipped; no changes made for this pair.")
            return None
    else:
        if left_asset_count >= right_asset_count:
            first_source, first_destination = left, right
            second_source, second_destination = right, left
        else:
            first_source, first_destination = right, left
            second_source, second_destination = left, right
        print(
            f"\t1. Replace {tag_value(first_source)!r} with "
            f"{tag_value(first_destination)!r} "
            f"({len(asset_cache[first_source['id']])} assets; "
            f"keep {tag_value(first_destination)!r})."
        )
        print(
            f"\t2. Replace {tag_value(second_source)!r} with "
            f"{tag_value(second_destination)!r} "
            f"({len(asset_cache[second_source['id']])} assets; "
            f"keep {tag_value(second_destination)!r})."
        )
        print("\t0. Skip this pair.")
        try:
            choice = input("\tChoose 0, 1, or 2: ").strip()
        except EOFError:
            choice = "0"

        if choice == "1":
            source_tag, destination_tag = first_source, first_destination
        elif choice == "2":
            source_tag, destination_tag = second_source, second_destination
        else:
            print("\tSkipped; no changes made for this pair.")
            return None

    source_id = source_tag["id"]
    source_assets = asset_cache[source_id]
    try:
        merge_tag_into(base_url, api_key, source_tag, destination_tag, source_assets)
    except ImmichApiError as error:
        print(f"\tCould not complete tag replacement: {error}")
        return None

    print(
        f"\tReplaced {tag_value(source_tag)!r} with "
        f"{tag_value(destination_tag)!r} on {len(source_assets)} assets; "
        f"deleted the source tag."
    )
    return {source_id}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List Immich tags with similar names and offer interactive replacement "
            "choices for matches above 90%; dotted tags are offered for merging "
            "into their undotted match."
        )
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD * 100,
        metavar="PERCENT",
        help="minimum name similarity percentage to report (default: 80)",
    )
    parser.add_argument(
        "--include-unmatched",
        action="store_true",
        help="also list tags that have no similar names at the chosen threshold",
    )
    args = parser.parse_args()
    if not 0 <= args.threshold <= 100:
        parser.error("--threshold must be between 0 and 100")
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
        tags = get_all_tags(base_url, api_key)
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    results = find_similar_tags(tags, args.threshold / 100)
    displayed = [
        (tag, matches)
        for tag, matches in results
        if matches or args.include_unmatched
    ]
    matching_count = sum(bool(matches) for _tag, matches in results)
    print(
        f"Loaded {len(tags)} tags; found {matching_count} tags with at least one "
        f"similar name at or above {args.threshold:g}%."
    )

    high_similarity_pairs: list[tuple[dict, dict, float]] = []
    seen_pair_ids: set[tuple[str, str]] = set()
    for tag, matches in displayed:
        print(f"{tag_value(tag)} (tag ID: {tag.get('id', 'unknown')})")
        if not matches:
            print("\tNo similar tag names found.")
            continue
        for other, score in matches:
            print(
                f"\t{score * 100:.1f}%: {tag_value(other)} "
                f"(tag ID: {other.get('id', 'unknown')})"
            )
            tag_id = tag.get("id")
            other_id = other.get("id")
            if (
                score > REPLACEMENT_PROMPT_THRESHOLD
                and isinstance(tag_id, str)
                and isinstance(other_id, str)
            ):
                pair_key = tuple(sorted((tag_id, other_id)))
                if pair_key not in seen_pair_ids:
                    seen_pair_ids.add(pair_key)
                    left, right = sorted(
                        (tag, other), key=lambda item: tag_value(item).casefold()
                    )
                    high_similarity_pairs.append((left, right, score))

    if not displayed:
        print("No tags met the selected similarity threshold.")

    asset_cache: dict[str, list[dict]] = {}
    merged_tag_ids: set[str] = set()
    for left, right, score in high_similarity_pairs:
        left_id = str(left.get("id"))
        right_id = str(right.get("id"))
        if left_id in merged_tag_ids or right_id in merged_tag_ids:
            print(
                f"\nSkipping {tag_value(left)!r} ↔ {tag_value(right)!r}: "
                "one of these tags was already deleted in an earlier replacement."
            )
            continue
        merged_ids = prompt_for_replacement(
            base_url, api_key, left, right, score, asset_cache
        )
        if merged_ids:
            merged_tag_ids.update(merged_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
