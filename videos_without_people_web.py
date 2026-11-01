#!/usr/bin/env python3
"""Serve a paginated web page of Immich videos with no tagged people."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

from people_comma_finder import ImmichApiError, api_request, display_base_url


IMMICH_PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 60


def parse_duration_text(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) == 1:
        seconds = float(parts[0])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        return round(seconds * 1000)
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        if minutes < 0 or not math.isfinite(seconds) or not 0 <= seconds < 60:
            raise ValueError
        return round((minutes * 60 + seconds) * 1000)
    if len(parts) == 3:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
        if (
            hours < 0
            or not 0 <= minutes < 60
            or not math.isfinite(seconds)
            or not 0 <= seconds < 60
        ):
            raise ValueError
        return round((hours * 3600 + minutes * 60 + seconds) * 1000)
    raise ValueError


def parse_duration_argument(value: str) -> int:
    try:
        return parse_duration_text(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "duration must be seconds, MM:SS, or HH:MM:SS (for example: 30, 2:30, 1:02:30)"
        ) from error


def duration_milliseconds(video: dict) -> int | None:
    """Read duration values from Immich's numeric and clock-string formats."""
    duration = video.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        try:
            return max(0, int(duration))
        except (OverflowError, ValueError):
            return None
    if not isinstance(duration, str) or not duration.strip():
        return None

    try:
        return parse_duration_text(duration)
    except ValueError:
        return None


def format_milliseconds(milliseconds: int) -> str:
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def format_duration(video: dict) -> str:
    milliseconds = duration_milliseconds(video)
    if milliseconds is None:
        return "Duration unknown"
    return format_milliseconds(milliseconds)


def load_thumbnail(base_url: str, api_key: str, asset_id: str) -> tuple[bytes, str]:
    url = f"{base_url}/api/assets/{quote(asset_id, safe='')}/thumbnail?size=thumbnail"
    request = Request(
        url,
        headers={"Accept": "image/*", "x-api-key": api_key},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read(), response.headers.get_content_type()
    except HTTPError as error:
        raise ImmichApiError(f"Immich returned HTTP {error.code} for video thumbnail {asset_id}") from error
    except URLError as error:
        raise ImmichApiError(f"Could not load video thumbnail {asset_id}: {error.reason}") from error


class UntaggedVideoCatalog:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        min_duration_ms: int | None = None,
        max_duration_ms: int | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self.videos: list[dict] = []
        self.immich_page = 1
        self.exhausted = False
        self.lock = threading.Lock()
        self.ready = False
        self.loading = False
        self.load_error: str | None = None

    def start_loading(self) -> None:
        with self.lock:
            if self.ready or self.loading or self.load_error:
                return
            self.loading = True
        threading.Thread(target=self._load_catalog, daemon=True).start()

    def _load_catalog(self) -> None:
        try:
            self._load_all()
        except ImmichApiError as error:
            with self.lock:
                self.load_error = str(error)
        finally:
            with self.lock:
                self.ready = self.exhausted and self.load_error is None
                self.loading = False

    def status(self) -> tuple[bool, bool, str | None]:
        with self.lock:
            return self.ready, self.loading, self.load_error

    def get_page(
        self, page: int, page_size: int, sort_order: str
    ) -> tuple[list[dict], bool, int]:
        start = (page - 1) * page_size
        end = start + page_size
        with self.lock:
            self._load_all()
            known_durations = [
                video for video in self.videos if duration_milliseconds(video) is not None
            ]
            unknown_durations = [
                video for video in self.videos if duration_milliseconds(video) is None
            ]
            known_durations.sort(
                key=duration_milliseconds,
                reverse=sort_order == "longest",
            )
            sorted_videos = known_durations + unknown_durations
            return sorted_videos[start:end], end < len(sorted_videos), len(sorted_videos)

    def _load_all(self) -> None:
        while not self.exhausted:
            self._load_until(len(self.videos) + 1)

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
                    duration = duration_milliseconds(video)
                    if duration is None and (
                        self.min_duration_ms is not None
                        or self.max_duration_ms is not None
                    ):
                        continue
                    if (
                        duration is not None
                        and self.min_duration_ms is not None
                        and duration < self.min_duration_ms
                    ):
                        continue
                    if (
                        duration is not None
                        and self.max_duration_ms is not None
                        and duration > self.max_duration_ms
                    ):
                        continue
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
    total_videos: int,
    sort_order: str,
    immich_url: str,
) -> str:
    entries = []
    first_number = (page - 1) * page_size + 1
    for index, video in enumerate(videos, start=first_number):
        video_id = str(video.get("id", ""))
        name = video.get("originalFileName") or video.get("originalPath") or video_id or "Unnamed video"
        url = f"{immich_url}/photos/{quote(video_id, safe='')}"
        entries.append(
            "<article class=\"video-card\">"
            f"<a class=\"thumbnail-link\" href=\"{html.escape(url, quote=True)}\" "
            "target=\"_blank\" rel=\"noopener noreferrer\">"
            f"<video class=\"thumbnail video-preview\" "
            f"poster=\"/thumbnail/{quote(video_id, safe='')}\" "
            f"src=\"/video/{quote(video_id, safe='')}\" "
            f"aria-label=\"Preview of {html.escape(str(name), quote=True)}\" "
            "muted playsinline loop preload=\"none\"></video>"
            "</a>"
            "<div class=\"video-details\">"
            f"<span class=\"number\">{index}.</span> "
            f"<a class=\"video-title\" href=\"{html.escape(url, quote=True)}\" "
            f"target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(str(name))}</a>"
            f"<span class=\"duration\">{html.escape(format_duration(video))}</span>"
            "</div></article>"
        )

    if not entries:
        listing = '<p class="empty">No videos without people tags were found on this page.</p>'
    else:
        listing = '<section class="videos" aria-label="Videos">' + "".join(entries) + "</section>"

    nav = []
    sort_query = f"&sort={quote(sort_order, safe='')}"
    if page > 1:
        nav.append(f'<a class="button" href="/?page={page - 1}{sort_query}">Previous</a>')
    if has_next:
        nav.append(f'<a class="button" href="/?page={page + 1}{sort_query}">Next</a>')
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
    .videos {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; }}
    .video-card {{ min-width: 0; overflow: hidden; border: 1px solid #8885; border-radius: .65rem; }}
    .thumbnail-link {{ display: block; background: #111; }}
    .thumbnail {{ display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; }}
    .video-details {{ display: flex; gap: .5rem; padding: .65rem .75rem; overflow-wrap: anywhere; }}
    .number {{ color: #888; flex: 0 0 auto; }}
    .video-title {{ min-width: 0; flex: 1; }}
    .duration {{ color: #888; flex: 0 0 auto; white-space: nowrap; }}
    .sort-control {{ display: flex; gap: .5rem; align-items: center; margin: 1rem 0; }}
    a {{ color: #4e9bff; }}
    nav {{ display: flex; gap: .75rem; align-items: center; margin: 1.5rem 0; }}
    .button {{ border: 1px solid #8888; border-radius: .4rem; padding: .45rem .8rem; text-decoration: none; }}
    .empty, .end {{ color: #888; }}
  </style>
</head>
<body>
  <h1>Videos without people tags ({total_videos})</h1>
  <p>Page {page} · {total_videos} videos total, {page_size} per page. Each video opens in Immich in a new tab.</p>
  <form class="sort-control" method="get" action="/">
    <label for="sort">Sort by duration:</label>
    <select id="sort" name="sort" onchange="this.form.submit()">
      <option value="shortest"{" selected" if sort_order == "shortest" else ""}>Shortest first</option>
      <option value="longest"{" selected" if sort_order == "longest" else ""}>Longest first</option>
    </select>
  </form>
  {listing}
  <nav aria-label="Pagination">{navigation}</nav>
  <script>
    document.querySelectorAll(".video-preview").forEach((video) => {{
      video.addEventListener("mouseenter", () => {{ video.play().catch(() => {{}}); }});
      video.addEventListener("mouseleave", () => {{
        video.pause();
        video.currentTime = 0;
      }});
    }});
  </script>
</body>
</html>"""


def render_loading_page(min_duration_ms: int | None, max_duration_ms: int | None) -> str:
    min_label = (
        format_milliseconds(min_duration_ms) if min_duration_ms is not None else "No minimum"
    )
    max_label = (
        format_milliseconds(max_duration_ms) if max_duration_ms is not None else "No maximum"
    )
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Counting Immich videos</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { min-height: 80vh; display: grid; place-content: center; justify-items: center; gap: 1rem; text-align: center; }
    .spinner { width: 2.5rem; height: 2.5rem; border: .3rem solid #8885; border-top-color: #4e9bff; border-radius: 50%; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="spinner" role="status" aria-label="Loading"></div>
  <p id="status">Counting the number of videos without people tags…</p>
  <p>Minimum duration: FILTER_MIN · Maximum duration: FILTER_MAX</p>
  <script>
    async function checkStatus() {
      try {
        const response = await fetch('/status', { cache: 'no-store' });
        const state = await response.json();
        if (state.error) {
          document.getElementById('status').textContent = 'Could not load videos: ' + state.error;
          document.querySelector('.spinner').hidden = true;
          return;
        }
        if (state.ready) {
          window.location.reload();
          return;
        }
      } catch (_) {
        // Retry while the catalog is loading.
      }
      window.setTimeout(checkStatus, 1000);
    }
    window.setTimeout(checkStatus, 500);
  </script>
</body>
</html>"""
    return page.replace("FILTER_MIN", html.escape(min_label)).replace(
        "FILTER_MAX", html.escape(max_label)
    )


def make_handler(catalog: UntaggedVideoCatalog, page_size: int, immich_url: str):
    class VideosHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/status":
                ready, loading, error = catalog.status()
                status_body = json.dumps(
                    {"ready": ready, "loading": loading, "error": error}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(status_body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(status_body)
                return

            if parsed.path.startswith("/thumbnail/"):
                asset_id = unquote(parsed.path.removeprefix("/thumbnail/"))
                if not asset_id or "/" in asset_id:
                    self.send_error(404)
                    return
                try:
                    image_data, content_type = load_thumbnail(
                        catalog.base_url, catalog.api_key, asset_id
                    )
                except ImmichApiError as error:
                    self.send_error(502, str(error))
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(image_data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(image_data)
                return

            if parsed.path.startswith("/video/"):
                asset_id = unquote(parsed.path.removeprefix("/video/"))
                if not asset_id or "/" in asset_id:
                    self.send_error(404)
                    return
                upstream_url = (
                    f"{catalog.base_url}/api/assets/{quote(asset_id, safe='')}"
                    "/video/playback"
                )
                upstream_headers = {"x-api-key": catalog.api_key}
                for header in ("Range", "If-Range"):
                    value = self.headers.get(header)
                    if value:
                        upstream_headers[header] = value
                request = Request(upstream_url, headers=upstream_headers)
                try:
                    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        self.send_response(response.status)
                        for header in (
                            "Content-Type",
                            "Content-Length",
                            "Content-Range",
                            "Accept-Ranges",
                            "ETag",
                            "Last-Modified",
                        ):
                            value = response.headers.get(header)
                            if value:
                                self.send_header(header, value)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        while chunk := response.read(64 * 1024):
                            self.wfile.write(chunk)
                except HTTPError as error:
                    self.send_error(error.code, "Immich could not stream this video")
                except (URLError, TimeoutError) as error:
                    self.send_error(502, f"Could not stream video from Immich: {error}")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            if parsed.path != "/":
                self.send_error(404)
                return

            catalog.start_loading()
            ready, _, error = catalog.status()
            if error:
                body = (
                    "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    "<title>Immich API error</title><body><h1>Could not load videos</h1>"
                    f"<pre>{html.escape(error)}</pre></body></html>"
                )
                status = 502
                encoded = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
                return
            if not ready:
                encoded = render_loading_page(
                    catalog.min_duration_ms, catalog.max_duration_ms
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)
                return

            try:
                page_value = parse_qs(parsed.query).get("page", ["1"])[0]
                page = max(1, int(page_value))
            except ValueError:
                page = 1
            sort_order = parse_qs(parsed.query).get("sort", ["shortest"])[0]
            if sort_order not in {"shortest", "longest"}:
                sort_order = "shortest"

            try:
                videos, has_next, total_videos = catalog.get_page(
                    page, page_size, sort_order
                )
                body = render_page(
                    videos, page, page_size, has_next, total_videos, sort_order, immich_url
                )
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
    parser.add_argument(
        "--min-duration",
        type=parse_duration_argument,
        metavar="DURATION",
        help="include videos at least this long (seconds, MM:SS, or HH:MM:SS)",
    )
    parser.add_argument(
        "--max-duration",
        type=parse_duration_argument,
        metavar="DURATION",
        help="include videos no longer than this (seconds, MM:SS, or HH:MM:SS)",
    )
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size must be between 1 and 100")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if (
        args.min_duration is not None
        and args.max_duration is not None
        and args.min_duration > args.max_duration
    ):
        parser.error("--min-duration cannot be greater than --max-duration")
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

    catalog = UntaggedVideoCatalog(
        base_url,
        api_key,
        min_duration_ms=args.min_duration,
        max_duration_ms=args.max_duration,
    )
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
