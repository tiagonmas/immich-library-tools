#!/usr/bin/env python3
"""List Immich images whose timeline date conflicts with folder or filename dates."""

from __future__ import annotations

import argparse
import calendar
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
FILENAME_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})"
    r"(?:[._-](?P<month>\d{1,2})(?:[._-](?P<day>\d{1,2}))?)?(?!\d)"
)
COMPACT_FILENAME_DATE_PATTERN = re.compile(r"(?<!\d)(?P<date>(?:19|20)\d{6})(?!\d)")


@dataclass(frozen=True)
class DateReference:
    expected_date: date
    precision: str
    source: str


@dataclass(frozen=True)
class DateMismatch:
    asset: dict
    original_path: str
    references: tuple[DateReference, ...]
    timeline_date: date | None
    reason: str


def parse_date_component(name: str, source: str) -> DateReference | None:
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
    return DateReference(expected, precision, source)


def folder_date_for_path(original_path: str) -> DateReference | None:
    """Use the nearest dated directory component in an Immich originalPath."""
    path = PurePosixPath(original_path)
    for component in reversed(path.parent.parts):
        folder_date = parse_date_component(component, f"folder '{component}'")
        if folder_date is not None:
            return folder_date
    return None


def filename_date_references(filename: str) -> list[DateReference]:
    references: list[DateReference] = []
    seen: set[tuple[date, str]] = set()
    matches = [
        (match, "separated")
        for match in FILENAME_DATE_PATTERN.finditer(filename)
    ] + [
        (match, "compact")
        for match in COMPACT_FILENAME_DATE_PATTERN.finditer(filename)
    ]
    for match, format_type in sorted(matches, key=lambda item: item[0].start()):
        if format_type == "compact":
            token = match.group("date")
            year, month, day = int(token[:4]), int(token[4:6]), int(token[6:8])
            precision = "day"
        else:
            year = int(match.group("year"))
            month_text = match.group("month")
            day_text = match.group("day")
            month = int(month_text) if month_text else None
            day = int(day_text) if day_text else None
            precision = (
                "day" if day is not None else "month" if month is not None else "year"
            )
        try:
            value = date(year, month or 1, day or 1)
        except ValueError:
            continue
        key = (value, precision)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            DateReference(
                value,
                precision,
                f"filename '{match.group(0)}'",
            )
        )
    return references


def parse_immich_datetime(value: object) -> datetime | None:
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
    return parsed


def parse_immich_date(value: object) -> date | None:
    parsed = parse_immich_datetime(value)
    return parsed.date() if parsed is not None else None


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
        filename = asset.get("originalFileName")
        if not isinstance(filename, str) or not filename.strip():
            return None
        original_path = filename

    references: list[DateReference] = []
    folder_reference = folder_date_for_path(original_path)
    if folder_reference is not None:
        references.append(folder_reference)
    filename = asset.get("originalFileName") or PurePosixPath(original_path).name
    if isinstance(filename, str):
        references.extend(filename_date_references(filename))
    if not references:
        return None

    timeline_date = parse_immich_date(asset.get("fileCreatedAt"))
    if timeline_date is None:
        return DateMismatch(
            asset=asset,
            original_path=original_path,
            references=tuple(references),
            timeline_date=None,
            reason="Immich has no valid fileCreatedAt timeline date",
        )

    reasons: list[str] = []
    for reference in references:
        if reference.precision == "year":
            if timeline_date.year != reference.expected_date.year:
                distance = abs((timeline_date - reference.expected_date).days)
                reasons.append(
                    f"{reference.source}: year mismatch; date is {distance} days away"
                )
        else:
            distance = abs((timeline_date - reference.expected_date).days)
            if distance > MAX_DATE_DISTANCE_DAYS:
                year_note = (
                    "year mismatch; "
                    if timeline_date.year != reference.expected_date.year
                    else ""
                )
                reasons.append(
                    f"{reference.source}: {year_note}date is {distance} days away"
                )
    if not reasons:
        return None
    return DateMismatch(
        asset=asset,
        original_path=original_path,
        references=tuple(references),
        timeline_date=timeline_date,
        reason="; ".join(reasons),
    )


def format_reference_date(reference: DateReference) -> str:
    if reference.precision == "year":
        return f"{reference.expected_date.year:04d}"
    if reference.precision == "month":
        return reference.expected_date.strftime("%Y-%m")
    return reference.expected_date.isoformat()


def replacement_datetime(
    reference: DateReference,
    current_datetime: datetime | None,
    references: tuple[DateReference, ...],
) -> datetime | None:
    if reference.precision == "year":
        target_year = reference.expected_date.year
        date_source = current_datetime.date() if current_datetime else None
        if date_source is None:
            more_precise = next(
                (
                    item
                    for item in references
                    if item.precision != "year"
                    and item.expected_date.year == target_year
                ),
                None,
            )
            if more_precise is None:
                return None
            date_source = more_precise.expected_date
        last_day = calendar.monthrange(target_year, date_source.month)[1]
        target_date = date(
            target_year, date_source.month, min(date_source.day, last_day)
        )
    else:
        target_date = reference.expected_date

    if current_datetime is not None:
        return datetime.combine(target_date, current_datetime.timetz())
    return datetime.combine(
        target_date, datetime.min.time().replace(hour=12, tzinfo=timezone.utc)
    )


def replacement_options(
    mismatch: DateMismatch,
) -> list[tuple[DateReference, datetime]]:
    current_datetime = parse_immich_datetime(mismatch.asset.get("fileCreatedAt"))
    options = []
    seen: set[tuple[date, str]] = set()
    for reference in mismatch.references:
        target = replacement_datetime(reference, current_datetime, mismatch.references)
        if target is None:
            continue
        key = (target.date(), reference.precision)
        if key in seen:
            continue
        seen.add(key)
        options.append((reference, target))
    return options


def replacement_options_conflict(
    options: list[tuple[DateReference, datetime]],
) -> bool:
    for index, (_, left_target) in enumerate(options):
        for _, right_target in options[index + 1 :]:
            if left_target.date() != right_target.date():
                return True
    return False


def update_immich_date(
    base_url: str, api_key: str, mismatch: DateMismatch
) -> None:
    asset_id = mismatch.asset.get("id")
    if not isinstance(asset_id, str):
        print("\tCannot update: Immich did not return an image ID.")
        return

    options = replacement_options(mismatch)
    if not options:
        print(
            "\tCannot choose a complete replacement date from the available "
            "folder/filename information; leaving the image unchanged."
        )
        return

    selected: tuple[DateReference, datetime]
    if replacement_options_conflict(options):
        print("\tFolder and filename date suggestions conflict:")
        for index, (reference, target) in enumerate(options, start=1):
            print(
                f"\t\t{index}. {target.isoformat()} "
                f"(from {reference.source}; {format_reference_date(reference)})"
            )
        try:
            choice = input("\tChoose a date number, or press Enter to skip: ").strip()
        except EOFError:
            choice = ""
        if not choice.isdigit() or not 1 <= int(choice) <= len(options):
            print("\tSkipped date update.")
            return
        selected = options[int(choice) - 1]
    else:
        selected = max(
            options,
            key=lambda option: (
                {"year": 0, "month": 1, "day": 2}[option[0].precision],
                option[0].source.startswith("filename"),
            ),
        )

    reference, target = selected
    current_value = mismatch.asset.get("fileCreatedAt")
    current_label = current_value if isinstance(current_value, str) else "unavailable"
    prompt = (
        f"\tUpdate Immich timeline date from {current_label} to "
        f"{target.isoformat()} using {reference.source}? [y/N] "
    )
    try:
        answer = input(prompt).strip().casefold()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        print("\tSkipped date update.")
        return

    encoded_id = quote(asset_id, safe="")
    try:
        api_request(
            base_url,
            api_key,
            f"/assets/{encoded_id}",
            {"dateTimeOriginal": target.isoformat(timespec="seconds")},
            method="PUT",
        )
    except ImmichApiError as error:
        print(f"\tDate update failed: {error}", file=sys.stderr)
        return
    print(f"\tUpdated Immich timeline date to {target.isoformat(timespec='seconds')}.")


def update_progress(current: int, total: int, folder: str) -> None:
    width = 24
    fraction = current / total if total else 1
    filled = int(width * fraction)
    bar = "=" * filled + " " * (width - filled)
    folder_label = folder
    if len(folder_label) > 56:
        folder_label = "..." + folder_label[-53:]
    sys.stdout.write(
        f"\rChecking [{bar}] {current}/{total} ({fraction:>5.0%}) "
        f"Folder: {folder_label}"
    )
    sys.stdout.flush()
    if current == total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List Immich images whose timeline dates do not match dates in "
            "their folders or filenames."
        )
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="list at most N results (folders with --listFolders; default: all)",
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
    parser.add_argument(
        "--ignoreFolder",
        action="append",
        metavar="TEXT",
        help=(
            "skip images whose folder path contains a directory with TEXT "
            "(case-insensitive); can be passed more than once"
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "ask before updating the mismatched date in Immich; by default, "
            "the script only reports mismatches"
        ),
    )
    parser.add_argument(
        "--listFolders",
        action="store_true",
        help="list only folders containing date-mismatched images",
    )
    args = parser.parse_args()
    if args.top is not None and args.top < 0:
        parser.error("--top must be zero or greater")
    if args.listFolders and args.replace:
        parser.error("--listFolders cannot be used with --replace")
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

    print("Scan scope: dates in all Immich image folder paths and filenames")
    print("Loading image records from Immich...")
    try:
        images = fetch_all_images(base_url, api_key)
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    mismatches: list[DateMismatch] = []
    checked_images = 0
    ignored_images = 0
    ignored_folder_images = 0
    skipped_without_date_reference = 0
    progress_interval = max(1, len(images) // 100)
    for index, image in enumerate(images, start=1):
        original_path = image.get("originalPath")
        current_folder = (
            str(PurePosixPath(original_path).parent)
            if isinstance(original_path, str)
            else "<unknown folder>"
        )
        if index == 1 or index == len(images) or index % progress_interval == 0:
            update_progress(
                index,
                len(images),
                "scanning folders" if args.listFolders else current_folder,
            )

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
        folder_parts = (
            PurePosixPath(original_path).parent.parts
            if isinstance(original_path, str)
            else ()
        )
        if args.ignoreFolder and any(
            ignored.casefold() in folder_part.casefold()
            for ignored in args.ignoreFolder
            for folder_part in folder_parts
        ):
            ignored_folder_images += 1
            continue

        has_folder_date = (
            isinstance(original_path, str)
            and folder_date_for_path(original_path) is not None
        )
        has_filename_date = bool(filename_date_references(filename))
        if not has_folder_date and not has_filename_date:
            skipped_without_date_reference += 1
            continue
        checked_images += 1
        mismatch = inspect_image(image)
        if mismatch is not None:
            mismatches.append(mismatch)

    if args.listFolders:
        mismatch_counts_by_folder: dict[str, int] = {}
        for mismatch in mismatches:
            folder = str(PurePosixPath(mismatch.original_path).parent)
            mismatch_counts_by_folder[folder] = (
                mismatch_counts_by_folder.get(folder, 0) + 1
            )
        selected_folders = sorted(
            mismatch_counts_by_folder.items(),
            key=lambda item: (-item[1], item[0].casefold()),
        )
        if args.top is not None:
            selected_folders = selected_folders[: args.top]
        print(
            f"Loaded {len(images)} images; checked {checked_images}; "
            f"found {len(mismatches)} date mismatches in "
            f"{len(mismatch_counts_by_folder)} folders; "
            f"listing {len(selected_folders)} folders."
        )
    else:
        selected = mismatches if args.top is None else mismatches[: args.top]
        print(
            f"Loaded {len(images)} images; checked {checked_images}; "
            f"found {len(mismatches)} date mismatches; "
            f"listing {len(selected)}."
        )
    if ignored_images:
        print(f"Ignored {ignored_images} images by filename.")
    if ignored_folder_images:
        print(f"Ignored {ignored_folder_images} images by folder name.")
    if skipped_without_date_reference:
        print(
            f"Skipped {skipped_without_date_reference} images whose folders and "
            "filenames do not contain a recognizable year or date."
        )

    web_base_url = display_base_url(base_url)
    if args.listFolders:
        for folder, mismatch_count in selected_folders:
            noun = "image" if mismatch_count == 1 else "images"
            print(f"Folder: {folder} ({mismatch_count} mismatched {noun})")
        return 0

    for mismatch in selected:
        asset_id = mismatch.asset.get("id")
        filename = mismatch.asset.get("originalFileName") or PurePosixPath(
            mismatch.original_path
        ).name
        print(f"Folder: {PurePosixPath(mismatch.original_path).parent}")
        print(f"\tImage: {filename}")
        for reference in mismatch.references:
            label = (
                "Folder date"
                if reference.source.startswith("folder ")
                else "Filename date"
            )
            print(
                f"\t{label}: {format_reference_date(reference)} "
                f"({reference.precision}; {reference.source})"
            )
        if mismatch.timeline_date is None:
            print("\tImmich timeline date: unavailable")
        else:
            print(f"\tImmich timeline date: {mismatch.timeline_date.isoformat()}")
        print(f"\tMismatch: {mismatch.reason}")
        if isinstance(asset_id, str):
            photo_url = f"{web_base_url}/photos/{quote(asset_id, safe='')}"
            print(f"\tOpen in Immich: {photo_url}")
        if args.replace:
            update_immich_date(base_url, api_key, mismatch)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
