#!/usr/bin/env python3
"""List Immich tags whose names contain a comma."""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlencode

from people_comma_finder import ImmichApiError, api_request


PAGE_SIZE = 1000


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
    matching_tags.sort(
        key=lambda tag: str(tag.get("value", tag.get("name", ""))).casefold()
    )

    print(
        f"Found {len(matching_tags)} tags with a comma in their name "
        f"(out of {len(tags)} tags loaded)."
    )
    for tag in matching_tags:
        name = tag.get("value", tag.get("name", ""))
        print(f"{name} (id: {tag.get('id', 'unknown')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
