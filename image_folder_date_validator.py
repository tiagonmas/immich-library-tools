#!/usr/bin/env python3
"""List Immich images whose timeline date conflicts with their folder date."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

from people_comma_finder import ImmichApiError, api_request, display_base_url


PAGE_SIZE = 1000
MAX_DATE_DISTANCE_DAYS = 60
FOLDER_DATE_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?:[._-](?P<month>\d{1,2})"
    r"(?:[._-](?P<day>\d{1,2}))?)?(?:\s|$)"
)


@dataclass(frozen=True)
class FolderDate:
    expected_date: date
    precision: str
    source_folder: str


@dataclass(frozen=True)
class DateMismatch:
    asset: dict
    original_path: str
    folder_date: FolderDate
    timeline_date: date | None
    reason: str


def parse_folder_component(name: str) -> FolderDate | None:
    match = FOLDER_DATE_PATTERN.match(name)
    if match is None:
        return None

    year = int(match.group("year"))
    month_text = match.group("month")
    day_text = match.group("day")
    month = int(month_text) if month_text else None
    day = int(day_text) if day_text else None
    precision = "day" if day is not None else "month" if month is not None else "year"
    try:
        expected = date(year, month or 1, day or 1)
    except ValueError:
        return None
    return FolderDate(expected, precision, name)


def folder_date_for_path(original_path: str) -> FolderDate | None:
    """Use the nearest dated directory component in an Immich originalPath."""
    path = PurePosixPath(original_path)
    for component in reversed(path.parent.parts):
        folder_date = parse_folder_component(component)
        if folder_date is not None:
            return folder_date
    return None


def parse_immich_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.date()


def fetch_all_images(base_url: str, api_key: str) -> list[dict]:
    images: list[dict] = []
    seen_ids: set[str] = set()
    page = 1

    while True:
        response = api_request(
            base_url,
            api_key,
            "/search/metadata",
            {"type": "IMAGE", "page": page, "size": PAGE_SIZE},
        )
        if not isinstance(response, dict):
            raise ImmichApiError("Unexpected response from Immich image search")
        assets = response.get("assets")
        if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
            raise ImmichApiError(
                "Unexpected response from Immich image search; expected assets.items"
            )

        page_items = [item for item in assets["items"] if isinstance(item, dict)]
        new_items = []
        for item in page_items:
            asset_id = item.get("id")
            if isinstance(asset_id, str):
                if asset_id not in seen_ids:
                    seen_ids.add(asset_id)
                    new_items.append(item)
            else:
                new_items.append(item)
        images.extend(new_items)

        has_next_page = assets.get("nextPage")
        if has_next_page is None:
            has_next_page = assets.get("hasNextPage", False)
        if not has_next_page:
            return images
        if not new_items:
            raise ImmichApiError(
                f"Image search pagination made no progress on page {page}"
            )

        page += 1
        time.sleep(0.05)


def inspect_image(asset: dict) -> DateMismatch | None:
    original_path = asset.get("originalPath")
    if not isinstance(original_path, str) or not original_path.strip():
        return None

    expected = folder_date_for_path(original_path)
    if expected is None:
        return None

    timeline_date = parse_immich_date(asset.get("fileCreatedAt"))
    if timeline_date is None:
        return DateMismatch(
            asset=asset,
            original_path=original_path,
            folder_date=expected,
            timeline_date=None,
            reason="Immich has no valid fileCreatedAt timeline date",
        )

    if timeline_date.year != expected.expected_date.year:
        distance = abs((timeline_date - expected.expected_date).days)
        return DateMismatch(
            asset,
            original_path,
            expected,
            timeline_date,
            f"year mismatch; date is {distance} days from the folder date",
        )

    if expected.precision != "year":
        distance = abs((timeline_date - expected.expected_date).days)
        if distance > MAX_DATE_DISTANCE_DAYS:
            return DateMismatch(
                asset,
                original_path,
                expected,
                timeline_date,
                f"date is {distance} days from the folder date",
            )
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List Immich images whose timeline dates do not match the year or "
            "dated folder they are stored in."
        )
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="list at most N mismatched images (default: all)",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        metavar="TEXT",
        help=(
            "skip images whose filename contains TEXT (case-insensitive); "
            "can be passed more than once"
        ),
    )
    args = parser.parse_args()
    if args.top is not None and args.top < 0:
        parser.error("--top must be zero or greater")
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
        images = fetch_all_images(base_url, api_key)
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    mismatches: list[DateMismatch] = []
    checked_images = 0
    ignored_images = 0
    skipped_without_folder_date = 0
    for image in images:
        original_path = image.get("originalPath")
        filename = image.get("originalFileName")
        if not isinstance(filename, str) or not filename:
            filename = (
                PurePosixPath(original_path).name
                if isinstance(original_path, str)
                else ""
            )
        if args.ignore and any(
            ignored.casefold() in filename.casefold() for ignored in args.ignore
        ):
            ignored_images += 1
            continue

        if not isinstance(original_path, str) or folder_date_for_path(original_path) is None:
            skipped_without_folder_date += 1
            continue
        checked_images += 1
        mismatch = inspect_image(image)
        if mismatch is not None:
            mismatches.append(mismatch)

    selected = mismatches if args.top is None else mismatches[: args.top]
    print(
        f"Loaded {len(images)} images; checked {checked_images}; "
        f"found {len(mismatches)} date mismatches; "
        f"listing {len(selected)}."
    )
    if ignored_images:
        print(f"Ignored {ignored_images} images by filename.")
    if skipped_without_folder_date:
        print(
            f"Skipped {skipped_without_folder_date} images whose folders do not "
            "contain a recognizable year or date."
        )

    web_base_url = display_base_url(base_url)
    for mismatch in selected:
        asset_id = mismatch.asset.get("id")
        filename = mismatch.asset.get("originalFileName") or PurePosixPath(
            mismatch.original_path
        ).name
        print(f"Folder: {PurePosixPath(mismatch.original_path).parent}")
        print(f"\tImage: {filename}")
        print(
            f"\tFolder date: {mismatch.folder_date.expected_date.isoformat()} "
            f"({mismatch.folder_date.precision}; from {mismatch.folder_date.source_folder})"
        )
        if mismatch.timeline_date is None:
            print("\tImmich timeline date: unavailable")
        else:
            print(f"\tImmich timeline date: {mismatch.timeline_date.isoformat()}")
        print(f"\tMismatch: {mismatch.reason}")
        if isinstance(asset_id, str):
            photo_url = f"{web_base_url}/photos/{quote(asset_id, safe='')}"
            print(f"\tOpen in Immich: {photo_url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
