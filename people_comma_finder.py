#!/usr/bin/env python3
"""Report images for comma-named Immich people and their tagged people."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 60


class ImmichApiError(RuntimeError):
    pass


def api_request(
    base_url: str,
    api_key: str,
    path: str,
    body: dict | None = None,
    *,
    method: str | None = None,
) -> object:
    url = f"{base_url}/api{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        method=method or ("POST" if payload is not None else "GET"),
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else None
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise ImmichApiError(f"Immich API returned HTTP {error.code} for {path}: {detail}") from error
    except URLError as error:
        raise ImmichApiError(f"Could not connect to Immich for {path}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise ImmichApiError(f"Immich returned invalid JSON for {path}") from error


def get_comma_named_people(base_url: str, api_key: str) -> list[dict]:
    response = api_request(base_url, api_key, "/people")
    if not isinstance(response, dict) or not isinstance(response.get("people"), list):
        raise ImmichApiError("Unexpected response from /people; expected an object with a people list")
    return [
        person
        for person in response["people"]
        if isinstance(person, dict)
        and isinstance(person.get("name"), str)
        and "," in person["name"]
    ]


def get_person_images(
    base_url: str, api_key: str, person_id: str, top: int | None = None
) -> list[dict]:
    """Fetch all image search pages for a person, including each image's people."""
    images: list[dict] = []
    page = 1

    while True:
        response = api_request(
            base_url,
            api_key,
            "/search/metadata",
            {
                "personIds": [person_id],
                "type": "IMAGE",
                "page": page,
                "size": min(PAGE_SIZE, top - len(images)) if top is not None else PAGE_SIZE,
                "withPeople": True,
            },
        )
        if not isinstance(response, dict):
            raise ImmichApiError(f"Unexpected image search response for person {person_id}")
        assets = response.get("assets")
        if not isinstance(assets, dict) or not isinstance(assets.get("items"), list):
            raise ImmichApiError(f"Unexpected assets response for person {person_id}")

        images.extend(asset for asset in assets["items"] if isinstance(asset, dict))
        if top is not None and len(images) >= top:
            return images[:top]
        if not assets.get("nextPage"):
            return images
        page += 1


def get_asset_faces(base_url: str, api_key: str, asset_id: str) -> list[dict]:
    query = urlencode({"id": asset_id})
    response = api_request(base_url, api_key, f"/faces?{query}")
    if not isinstance(response, list):
        raise ImmichApiError(f"Unexpected faces response for asset {asset_id}")
    return [face for face in response if isinstance(face, dict)]


def remove_person_faces(
    base_url: str,
    api_key: str,
    asset_id: str,
    person_id: str,
    person_name: str,
) -> int:
    faces = get_asset_faces(base_url, api_key, asset_id)
    matching_faces = [
        face
        for face in faces
        if (
            face.get("personId") == person_id
            or (
                isinstance(face.get("person"), dict)
                and face["person"].get("id") == person_id
            )
        )
        and isinstance(face.get("id"), str)
    ]
    for face in matching_faces:
        face_id = face["id"]
        api_request(
            base_url,
            api_key,
            f"/faces/{quote(face_id, safe='')}",
            {"force": False},
            method="DELETE",
        )
        print(f"\t\tRemoved face tag for {person_name} (face id: {face_id})")
    if not matching_faces:
        print(f"\t\tNo face entry for {person_name} was found to remove.")
    return len(matching_faces)


def individual_names(person_name: str) -> list[str]:
    # Names in this Immich naming convention are comma-delimited. Trim whitespace
    # and ignore empty pieces in case a name contains an extra comma or spaces.
    return [name.strip() for name in person_name.split(",") if name.strip()]


def normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def display_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return base_url

    host = "192.168.1.200"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit(parsed._replace(netloc=netloc))


def tagged_people(image: dict) -> list[dict]:
    people = image.get("people")
    if not isinstance(people, list):
        return []
    return [person for person in people if isinstance(person, dict)]


def check_person(
    base_url: str,
    api_key: str,
    person: dict,
    image_limit: int | None,
    tag_status: str,
    dry_run: bool,
    confirm: bool,
) -> int:
    name = person["name"]
    person_id = person.get("id")
    if not isinstance(person_id, str) or not person_id:
        print(f"\nSkipping {name!r}: person record has no id", file=sys.stderr)
        return 0

    expected_names = individual_names(name)
    expected_normalized = {normalized_name(value) for value in expected_names}
    print(f"\nPerson: {name} (id: {person_id})")
    print(f"\tIndividual names to find: {', '.join(expected_names)}")

    images = get_person_images(base_url, api_key, person_id, image_limit)
    if not images:
        print("\tNo associated images found.")
        return 0

    shown = 0
    for image in images:
        image_id = image.get("id", "unknown id")
        image_name = image.get("originalFileName") or image.get("originalPath") or image_id
        people = tagged_people(image)
        tagged_names = [
            tagged.get("name")
            for tagged in people
            if isinstance(tagged.get("name"), str) and tagged["name"].strip()
        ]
        tagged_normalized = {normalized_name(tagged_name) for tagged_name in tagged_names}
        missing = [
            expected
            for expected in expected_names
            if normalized_name(expected) not in tagged_normalized
        ]
        all_names_tagged = not missing
        if tag_status == "yes" and not all_names_tagged:
            continue
        if tag_status == "no" and all_names_tagged:
            continue

        print(f"\tLoading image: {image_name} (id: {image_id})")
        image_url = f"{display_base_url(base_url)}/photos/{quote(str(image_id), safe='')}"
        print(f"\t\tOpen in Immich: {image_url}")
        print(f"\t\tTagged people ({len(people)}): {', '.join(tagged_names) if tagged_names else '(none)'}")
        if missing:
            print(f"\t\tAll individual names tagged: no; missing: {', '.join(missing)}")
        else:
            print("\t\tAll individual names tagged: yes")
        if dry_run:
            print(f"\t\tDry run: would remove the face tag for {name} from this image.")
        else:
            should_remove = True
            if confirm:
                try:
                    answer = input(
                        f"\t\tRemove {name} from this image? [y/N] "
                    ).strip().casefold()
                except EOFError:
                    answer = ""
                should_remove = answer in {"y", "yes"}
            if should_remove:
                remove_person_faces(base_url, api_key, str(image_id), person_id, name)
            else:
                print(f"\t\tSkipped removal for {name}.")
        shown += 1
    if shown == 0:
        print(f"\tNo images matched --tag-status {tag_status}.")
    return len(images)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report tags on images associated with comma-named Immich people."
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="inspect at most N images total across the run (default: inspect all images)",
    )
    parser.add_argument(
        "--list-people",
        action="store_true",
        help="list people with commas in their names and exit without searching images",
    )
    parser.add_argument(
        "--tag-status",
        choices=("yes", "no", "all"),
        default="all",
        help='show images where all individual names are tagged: "yes", "no", or "all" (default: all)',
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="preview removal of the comma-named person's face from fully tagged images (default; use --no-dry-run to apply)",
    )
    parser.add_argument(
        "--confirm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ask before each image change (default; use --no-confirm to skip prompts)",
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
        if not args.list_people:
            print(f"Mode: {'dry run' if args.dry_run else 'LIVE: changes will be applied'}")
        people = get_comma_named_people(base_url, api_key)
        print(f"Found {len(people)} people with a comma in their name.")
        if args.list_people:
            for person in people:
                print(f"{person['name']} (id: {person.get('id', 'unknown')})")
            return 0

        images_processed = 0
        for person in people:
            if args.top is not None and images_processed >= args.top:
                break
            remaining = args.top - images_processed if args.top is not None else None
            images_processed += check_person(
                base_url,
                api_key,
                person,
                remaining,
                args.tag_status,
                args.dry_run,
                args.confirm,
            )
            # Keep requests sequential and give the server a short breather.
            if args.top is None or images_processed < args.top:
                time.sleep(0.05)
        if args.top is not None:
            print(f"\nStopped after inspecting {images_processed} of {args.top} requested images.")
    except ImmichApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
