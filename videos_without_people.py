#!/usr/bin/env python3
"""List Immich videos that have no people tagged."""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import quote

from people_comma_finder import ImmichApiError, api_request, display_base_url


PAGE_SIZE = 100


def get_videos(base_url: str, api_key: str) -> list[dict]:
    videos: list[dict] = []
    page = 1

    while True:
        response = api_request(
            base_url,
            api_key,
            "/search/metadata",
            {
                "type": "VIDEO",
                "page": page,
                "size": PAGE_SIZE,
                "withPeople": True,
            },
        )
        if not isinstance(response, dict):
            raise ImmichApiError("Unexpected video search response")
        assets = response.get("assets")
        if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
            raise ImmichApiError("Unexpected assets response from video search")

        videos.extend(video for video in assets["items"] if isinstance(video, dict))
        if not assets.get("nextPage"):
            return videos
        page += 1
        time.sleep(0.05)


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
        videos = get_videos(base_url, api_key)
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    for video in videos:
        if not isinstance(video.get("people"), list):
            print(
                f"Error: video {video.get('id', 'unknown id')} is missing people data.",
                file=sys.stderr,
            )
            return 1

    untagged_videos = [video for video in videos if len(video["people"]) == 0]
    print(f"Found {len(untagged_videos)} videos with no people tagged.")
    for video in untagged_videos:
        video_id = str(video.get("id", "unknown id"))
        video_name = video.get("originalFileName") or video.get("originalPath") or video_id
        video_url = f"{display_base_url(base_url)}/photos/{quote(video_id, safe='')}"
        print(f"{video_name} (id: {video_id})")
        print(f"\tOpen in Immich: {video_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
