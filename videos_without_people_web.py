#!/usr/bin/env python3
"""Serve a paginated web page of Immich videos with no tagged people."""

from __future__ import annotations

import argparse
import html
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from people_comma_finder import ImmichApiError, api_request, display_base_url


IMMICH_PAGE_SIZE = 100


class UntaggedVideoCatalog:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.videos: list[dict] = []
        self.immich_page = 1
        self.exhausted = False
        self.lock = threading.Lock()

    def get_page(self, page: int, page_size: int) -> tuple[list[dict], bool]:
        start = (page - 1) * page_size
        end = start + page_size
        with self.lock:
            self._load_until(end + 1)
            return self.videos[start:end], len(self.videos) > end or not self.exhausted

    def _load_until(self, count: int) -> None:
        while len(self.videos) < count and not self.exhausted:
            response = api_request(
                self.base_url,
                self.api_key,
                "/search/metadata",
                {
                    "type": "VIDEO",
                    "page": self.immich_page,
                    "size": IMMICH_PAGE_SIZE,
                    "withPeople": True,
                },
            )
            if not isinstance(response, dict):
                raise ImmichApiError("Unexpected video search response")
            assets = response.get("assets")
            if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
                raise ImmichApiError("Unexpected assets response from video search")

            for video in assets["items"]:
                if not isinstance(video, dict):
                    continue
                people = video.get("people")
                if not isinstance(people, list):
                    raise ImmichApiError(
                        f"Video {video.get('id', 'unknown id')} is missing people data"
                    )
                if not people:
                    self.videos.append(video)

            if assets.get("nextPage"):
                self.immich_page += 1
            else:
                self.exhausted = True


def render_page(
    videos: list[dict],
    page: int,
    page_size: int,
    has_next: bool,
    immich_url: str,
) -> str:
    entries = []
    first_number = (page - 1) * page_size + 1
    for index, video in enumerate(videos, start=first_number):
        video_id = str(video.get("id", ""))
        name = video.get("originalFileName") or video.get("originalPath") or video_id or "Unnamed video"
        url = f"{immich_url}/photos/{quote(video_id, safe='')}"
        entries.append(
            "<li>"
            f"<span class=\"number\">{index}.</span> "
            f"<a href=\"{html.escape(url, quote=True)}\" target=\"_blank\" "
            f"rel=\"noopener noreferrer\">{html.escape(str(name))}</a>"
            "</li>"
        )

    if not entries:
        listing = '<p class="empty">No videos without people tags were found on this page.</p>'
    else:
        listing = "<ol class=\"videos\">" + "".join(entries) + "</ol>"

    nav = []
    if page > 1:
        nav.append(f'<a class="button" href="/?page={page - 1}">Previous</a>')
    if has_next:
        nav.append(f'<a class="button" href="/?page={page + 1}">Next</a>')
    navigation = " ".join(nav) if nav else '<span class="end">End of results</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Immich videos without people</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ max-width: 850px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
    h1 {{ font-size: 1.65rem; }}
    .videos {{ padding-left: 2.2rem; }}
    .videos li {{ padding: .55rem 0; overflow-wrap: anywhere; }}
    .number {{ color: #888; margin-left: -1.9rem; display: inline-block; width: 1.7rem; }}
    a {{ color: #4e9bff; }}
    nav {{ display: flex; gap: .75rem; align-items: center; margin: 1.5rem 0; }}
    .button {{ border: 1px solid #8888; border-radius: .4rem; padding: .45rem .8rem; text-decoration: none; }}
    .empty, .end {{ color: #888; }}
  </style>
</head>
<body>
  <h1>Videos without people tags</h1>
  <p>Page {page} · up to {page_size} videos. Each video opens in Immich in a new tab.</p>
  {listing}
  <nav aria-label="Pagination">{navigation}</nav>
</body>
</html>"""


def make_handler(catalog: UntaggedVideoCatalog, page_size: int, immich_url: str):
    class VideosHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/":
                self.send_error(404)
                return

            try:
                page_value = parse_qs(parsed.query).get("page", ["1"])[0]
                page = max(1, int(page_value))
            except ValueError:
                page = 1

            try:
                videos, has_next = catalog.get_page(page, page_size)
                body = render_page(videos, page, page_size, has_next, immich_url)
                status = 200
            except ImmichApiError as error:
                body = (
                    "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    "<title>Immich API error</title><body><h1>Could not load videos</h1>"
                    f"<pre>{html.escape(str(error))}</pre></body></html>"
                )
                status = 502

            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format_string: str, *args: object) -> None:
            sys.stdout.write(f"{self.address_string()} - {format_string % args}\n")

    return VideosHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a paginated list of Immich videos with no people tagged."
    )
    parser.add_argument("--host", default="127.0.0.1", help="web server bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="web server port (default: 8765)")
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="number of matching videos per page (default: 20; max: 100)",
    )
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size must be between 1 and 100")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
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

    catalog = UntaggedVideoCatalog(base_url, api_key)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(catalog, args.page_size, display_base_url(base_url)),
    )
    print(f"Serving at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
