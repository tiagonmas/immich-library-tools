#!/usr/bin/env python3
"""List comma-named Immich tags and optionally replace them on images."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from people_comma_finder import ImmichApiError, api_request


PAGE_SIZE = 1000
WRITE_BATCH_SIZE = 100


def parse_datetime(value: str) -> datetime:
    """Parse an ISO date or datetime, interpreting timezone-free values as UTC."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "date must be ISO format, such as 2026-10-31 or "
            "2026-10-31T22:12:43Z"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_tag_created_at(tag: dict) -> datetime | None:
    created_at = tag.get("createdAt")
    if not isinstance(created_at, str):
        return None
    try:
        return parse_datetime(created_at)
    except argparse.ArgumentTypeError:
        return None


def get_all_tags(base_url: str, api_key: str) -> list[dict]:
    """Fetch all tags, following pagination when the API provides it."""
    tags: list[dict] = []
    known_ids: set[str] = set()
    total_tags: int | None = None
    page = 1

    while True:
        query = urlencode({"page": page, "size": PAGE_SIZE})
        response = api_request(base_url, api_key, f"/tags?{query}")

        # Immich currently returns all tags as a JSON array. Treat that as the
        # complete collection; some versions or compatible servers may instead
        # return a paginated object.
        if isinstance(response, list):
            return [tag for tag in response if isinstance(tag, dict)]

        if not isinstance(response, dict):
            raise ImmichApiError("Unexpected response from /tags")

        page_tags = response.get("tags", response.get("items"))
        if not isinstance(page_tags, list):
            raise ImmichApiError(
                "Unexpected response from /tags; expected a list of tags"
            )

        previous_count = len(known_ids)
        for tag in page_tags:
            if not isinstance(tag, dict):
                continue
            tag_id = tag.get("id")
            if isinstance(tag_id, str):
                if tag_id not in known_ids:
                    known_ids.add(tag_id)
                    tags.append(tag)
            else:
                tags.append(tag)

        response_total = response.get("total")
        if isinstance(response_total, int):
            total_tags = response_total

        has_next_page = response.get("hasNextPage")
        if "nextPage" in response:
            next_page = response.get("nextPage")
            has_next_page = next_page is not None and next_page is not False

        if total_tags is not None and len(known_ids) >= total_tags:
            break
        if has_next_page is False:
            if total_tags is None or len(known_ids) >= total_tags:
                break
        elif has_next_page is not True and len(page_tags) < PAGE_SIZE:
            break

        if len(known_ids) == previous_count:
            raise ImmichApiError(
                f"Immich /tags pagination stopped making progress on page {page}; "
                "unable to confirm that all tags were loaded"
            )

        page += 1
        time.sleep(0.05)

    return tags


def get_asset_page(response: object) -> tuple[list[dict], object, object]:
    if not isinstance(response, dict):
        raise ImmichApiError("Unexpected response from Immich asset search")
    assets = response.get("assets")
    if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
        raise ImmichApiError(
            "Unexpected response from Immich asset search; expected assets.items"
        )
    items = [item for item in assets["items"] if isinstance(item, dict)]
    return items, assets.get("nextPage"), assets.get("nextCursor")


def get_tag_images(base_url: str, api_key: str, tag_id: str) -> list[dict]:
    """Fetch matching images through Immich's structured cursor search API."""
    assets: list[dict] = []
    known_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    while True:
        body: dict[str, object] = {
            "filter": {
                "tagIds": {"any": [tag_id]},
                "type": {"eq": "IMAGE"},
            },
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


def tag_value(tag: dict) -> str:
    value = tag.get("value", tag.get("name", ""))
    return value if isinstance(value, str) else ""


def split_tag_value(value: str) -> list[str]:
    return list(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )


def get_tag_ids_by_value(tags: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for tag in tags:
        value = tag_value(tag)
        tag_id = tag.get("id")
        if value and isinstance(tag_id, str):
            result[value] = tag_id
    return result


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def replace_tag_on_images(
    base_url: str,
    api_key: str,
    tag: dict,
    all_tags: list[dict],
    *,
    dry_run: bool,
    confirm: bool,
) -> None:
    name = tag_value(tag)
    tag_id = tag.get("id")
    if not isinstance(tag_id, str):
        raise ImmichApiError(f"Tag {name!r} has no ID; cannot replace it")

    replacement_names = split_tag_value(name)
    if not replacement_names:
        print(f"\tNo individual tag names found in {name!r}; skipping.")
        return

    print(f"\tIndividual tags: {', '.join(replacement_names)}")
    assets = get_tag_images(base_url, api_key, tag_id)
    asset_ids = [asset["id"] for asset in assets]
    print(f"\tImages using this tag: {len(asset_ids)}")
    for asset in assets:
        filename = asset.get("originalFileName") or "unknown filename"
        print(f"\t\tPhoto ID: {asset['id']} (filename: {filename})")

    tag_ids_by_value = get_tag_ids_by_value(all_tags)
    missing_names = [
        replacement_name
        for replacement_name in replacement_names
        if replacement_name not in tag_ids_by_value
    ]

    if dry_run:
        if missing_names and asset_ids:
            print(f"\tWould create missing tags: {', '.join(missing_names)}")
        if asset_ids:
            print(
                f"\tWould add all individual tags to {len(asset_ids)} images, "
                f"then delete the original tag {name!r}."
            )
        else:
            print(f"\tWould delete the original tag {name!r}; no images use it.")
        print(
            "\tDeleting the tag also removes it from any videos using it; "
            "replacement tags are added to images only."
        )
        return

    if confirm:
        question = (
            f"\tAdd replacement tags to {len(asset_ids)} images and delete "
            f"the original tag {name!r}?"
            if asset_ids
            else f"\tDelete the original tag {name!r}? No images use it."
        )
        try:
            answer = input(
                question
                + " This also removes it from any videos using it. [y/N] "
            )
        except EOFError:
            answer = ""
        if answer.strip().casefold() not in {"y", "yes"}:
            print("\tSkipped; no changes made for this tag.")
            return

    if not asset_ids:
        api_request(
            base_url,
            api_key,
            f"/tags/{quote(tag_id, safe='')}",
            method="DELETE",
        )
        print(f"\tDeleted original tag {name!r}.")
        return

    if missing_names:
        response = api_request(
            base_url,
            api_key,
            "/tags",
            {"tags": missing_names},
            method="PUT",
        )
        if not isinstance(response, list):
            raise ImmichApiError("Unexpected response while creating replacement tags")
        tag_ids_by_value.update(get_tag_ids_by_value(response))

    missing_ids = [
        replacement_name
        for replacement_name in replacement_names
        if replacement_name not in tag_ids_by_value
    ]
    if missing_ids:
        raise ImmichApiError(
            "Immich did not return IDs for replacement tags: "
            + ", ".join(missing_ids)
        )

    replacement_ids = [tag_ids_by_value[value] for value in replacement_names]
    for asset_batch in chunks(asset_ids, WRITE_BATCH_SIZE):
        api_request(
            base_url,
            api_key,
            "/tags/assets",
            {"tagIds": replacement_ids, "assetIds": asset_batch},
            method="PUT",
        )
    print(f"\tAdded individual tags to {len(asset_ids)} images.")

    api_request(
        base_url,
        api_key,
        f"/tags/{quote(tag_id, safe='')}",
        method="DELETE",
    )
    print(f"\tDeleted original tag {name!r}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Immich tags whose names contain a comma."
    )
    parser.add_argument(
        "--sort-created-at",
        choices=("asc", "desc"),
        help="sort matching tags by createdAt in ascending or descending order",
    )
    parser.add_argument(
        "--created-after",
        type=parse_datetime,
        metavar="DATE",
        help=(
            "include only tags created strictly after this ISO date or datetime "
            "(timezone-free values are interpreted as UTC)"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help=(
            "list or process at most N comma-named tags after filtering and sorting"
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "replace comma-named tag assignments on images with the individual "
            "comma-separated tags"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show planned replacement changes without updating Immich (default)",
    )
    parser.add_argument(
        "--confirm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "ask before replacing each tag in live mode (default); use --no-confirm "
            "to apply without prompts"
        ),
    )
    args = parser.parse_args()
    args.confirm_was_explicit = "--confirm" in sys.argv[1:]
    if args.top is not None and args.top < 0:
        parser.error("--top must be zero or greater")
    if not args.dry_run and not args.replace:
        parser.error("--no-dry-run requires --replace")
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

    matching_tags = [
        tag
        for tag in tags
        if isinstance(tag.get("value", tag.get("name")), str)
        and "," in tag.get("value", tag.get("name"))
    ]
    if args.created_after is not None:
        matching_tags = [
            tag
            for tag in matching_tags
            if (created_at := get_tag_created_at(tag)) is not None
            and created_at > args.created_after
        ]

    if args.sort_created_at:
        dated_tags = [tag for tag in matching_tags if get_tag_created_at(tag) is not None]
        undated_tags = [tag for tag in matching_tags if get_tag_created_at(tag) is None]
        dated_tags.sort(
            key=lambda tag: (
                get_tag_created_at(tag),
                str(tag.get("value", tag.get("name", ""))).casefold(),
            ),
            reverse=args.sort_created_at == "desc",
        )
        matching_tags = dated_tags + undated_tags
    else:
        matching_tags.sort(
            key=lambda tag: str(tag.get("value", tag.get("name", ""))).casefold()
        )

    total_matching_tags = len(matching_tags)
    if args.top is not None:
        matching_tags = matching_tags[: args.top]

    print(
        f"Found {total_matching_tags} tags with a comma in their name; "
        f"processing {len(matching_tags)} (out of {len(tags)} tags loaded)."
    )
    if args.replace:
        mode = (
            "DRY RUN: no changes will be applied"
            if args.dry_run
            else "LIVE: changes will be applied"
        )
        if not args.dry_run:
            mode += "; per-tag confirmation " + ("enabled" if args.confirm else "disabled")
        print(f"Replacement mode: {mode}")
    if args.replace and args.dry_run and args.confirm_was_explicit:
        print(
            "Warning: --confirm has no effect in dry-run mode. "
            "Use --no-dry-run to apply changes and be prompted."
        )

    for tag in matching_tags:
        name = tag_value(tag)
        print(
            f"{name} (tag ID: {tag.get('id', 'unknown')}; "
            f"createdAt: {tag.get('createdAt', 'unknown')})"
        )
        if args.replace:
            try:
                replace_tag_on_images(
                    base_url,
                    api_key,
                    tag,
                    tags,
                    dry_run=args.dry_run,
                    confirm=args.confirm,
                )
            except ImmichApiError as error:
                print(f"\tError: {error}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
