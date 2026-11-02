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


def get_all_people(base_url: str, api_key: str) -> list[dict]:
    """Fetch every page from Immich's paginated people endpoint."""
    people: list[dict] = []
    known_ids: set[str] = set()
    total_people: int | None = None
    page = 1

    while True:
        query = urlencode({"page": page, "size": 1000, "withHidden": "true"})
        response = api_request(base_url, api_key, f"/people?{query}")
        if not isinstance(response, dict) or not isinstance(response.get("people"), list):
            raise ImmichApiError(
                "Unexpected response from /people; expected an object with a people list"
            )

        page_people = [person for person in response["people"] if isinstance(person, dict)]
        previous_count = len(known_ids)
        for person in page_people:
            person_id = person.get("id")
            if isinstance(person_id, str):
                if person_id not in known_ids:
                    known_ids.add(person_id)
                    people.append(person)
            else:
                people.append(person)

        response_total = response.get("total")
        if isinstance(response_total, int):
            total_people = response_total

        has_next_page = response.get("hasNextPage")
        if total_people is not None and len(known_ids) >= total_people:
            break
        if has_next_page is False:
            # Some Immich versions report a total larger than the page size can
            # return. Keep paging until that total is collected, when possible.
            if total_people is None or len(known_ids) >= total_people:
                break
        elif has_next_page is not True and len(known_ids) == previous_count:
            break

        if len(known_ids) == previous_count:
            expected = f" of {total_people}" if total_people is not None else ""
            raise ImmichApiError(
                f"Immich /people pagination stopped making progress on page {page}; "
                f"received {len(known_ids)}{expected} people. The collection endpoint "
                "is not returning all people; GET /people/{id} can still return an "
                "individual record that is absent from this list."
            )
        page += 1

    return people


def get_comma_named_people(
    base_url: str,
    api_key: str,
    include_person_ids: list[str] | None = None,
) -> list[dict]:
    """Find comma-named people through similar-name searches for every known name."""
    seed_people = get_all_people(base_url, api_key)
    people_by_id = {
        person["id"]: person
        for person in seed_people
        if isinstance(person, dict)
        and isinstance(person.get("id"), str)
        and isinstance(person.get("name"), str)
        and person["name"].strip()
    }

    for person_id in include_person_ids or []:
        if person_id not in people_by_id:
            person = api_request(base_url, api_key, f"/people/{quote(person_id, safe='')}")
            if not isinstance(person, dict):
                raise ImmichApiError(f"Unexpected response from /people/{person_id}")
            if isinstance(person.get("name"), str) and person["name"].strip():
                people_by_id[person_id] = person

    seed_names = list(dict.fromkeys(person["name"].strip() for person in people_by_id.values()))
    similar_people_by_id = dict(people_by_id)
    print(f"Loaded {len(seed_names)} distinct named people; searching for similar names.", flush=True)

    for index, name in enumerate(seed_names, start=1):
        query = urlencode({"name": name, "withHidden": "true"})
        response = api_request(base_url, api_key, f"/search/person?{query}")
        if not isinstance(response, list):
            raise ImmichApiError(
                f"Unexpected response from /search/person for name {name!r}; expected a list"
            )
        for person in response:
            if isinstance(person, dict) and isinstance(person.get("id"), str):
                similar_people_by_id[person["id"]] = person

        if index == 1 or index % 100 == 0 or index == len(seed_names):
            print(f"Similar-name searches: {index}/{len(seed_names)}", flush=True)

    return [
        person
        for person in similar_people_by_id.values()
        if isinstance(person, dict)
        and isinstance(person.get("name"), str)
        and "," in person["name"]
    ]


def get_person_asset_count(base_url: str, api_key: str, person_id: str) -> int:
    response = api_request(
        base_url,
        api_key,
        f"/people/{quote(person_id, safe='')}/statistics",
    )
    if not isinstance(response, dict) or not isinstance(response.get("assets"), int):
        raise ImmichApiError(
            f"Unexpected response from /people/{person_id}/statistics; expected an assets count"
        )
    return response["assets"]


def hide_person(base_url: str, api_key: str, person_id: str) -> None:
    api_request(
        base_url,
        api_key,
        f"/people/{quote(person_id, safe='')}",
        {"isHidden": True},
        method="PUT",
    )


def delete_person(base_url: str, api_key: str, person_id: str) -> None:
    api_request(
        base_url,
        api_key,
        f"/people/{quote(person_id, safe='')}",
        {},
        method="DELETE",
    )


def format_person_summary(person: dict) -> str:
    summary = (
        f"{person['name']} (id: {person.get('id', 'unknown')}; "
        f"assets: {person.get('assetCount', 0)})"
    )
    if person.get("assetCount", 0) > 0:
        return f"\033[31m{summary}\033[0m"
    return summary


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
    print(f"\nPerson: {format_person_summary(person)}")
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
        "--include-person-id",
        action="append",
        default=[],
        metavar="ID",
        help="also fetch this known person by ID when --list-people (repeatable; useful for people omitted from /people)",
    )
    parser.add_argument(
        "--hide-people",
        action="store_true",
        help="with --list-people, hide comma-named people that have zero associated assets",
    )
    parser.add_argument(
        "--delete-people",
        action="store_true",
        help="with --list-people, delete all identified people with commas in their names",
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
        help="preview changes without updating Immich (default; use --no-dry-run to apply)",
    )
    parser.add_argument(
        "--confirm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="ask before each image or person change (default; use --no-confirm to skip prompts)",
    )
    args = parser.parse_args()
    if args.top is not None and args.top < 0:
        parser.error("--top must be zero or greater")
    if args.include_person_id and not args.list_people:
        parser.error("--include-person-id requires --list-people")
    if args.hide_people and not args.list_people:
        parser.error("--hide-people requires --list-people")
    if args.delete_people and not args.list_people:
        parser.error("--delete-people requires --list-people")
    if args.hide_people and args.delete_people:
        parser.error("--hide-people and --delete-people cannot be used together")
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
        if not args.list_people or args.hide_people or args.delete_people:
            print(f"Mode: {'dry run' if args.dry_run else 'LIVE: changes will be applied'}")
        people = get_comma_named_people(
            base_url,
            api_key,
            include_person_ids=args.include_person_id,
        )
        for person in people:
            person_id = person.get("id")
            if not isinstance(person_id, str) or not person_id:
                raise ImmichApiError(f"Person {person.get('name', '(unnamed)')!r} has no id")
            person["assetCount"] = get_person_asset_count(base_url, api_key, person_id)
        print(f"Found {len(people)} people with a comma in their name.")
        if args.list_people:
            for person in people:
                print(format_person_summary(person))
                if args.delete_people:
                    person_id = person["id"]
                    if args.dry_run:
                        print("\tDry run: would delete this comma-named person and unassign its faces.")
                        continue

                    should_delete = True
                    if args.confirm:
                        try:
                            answer = input("\tDelete this person? [y/N] ").strip().casefold()
                        except EOFError:
                            answer = ""
                        should_delete = answer in {"y", "yes"}
                    if should_delete:
                        delete_person(base_url, api_key, person_id)
                        print("\tPerson deleted; its faces are unassigned.")
                    else:
                        print("\tSkipped deleting this person.")
                    continue

                if not args.hide_people or person.get("assetCount", 0) != 0:
                    continue

                person_id = person["id"]
                if person.get("isHidden") is True:
                    print("\tAlready hidden.")
                    continue
                if args.dry_run:
                    print("\tDry run: would hide this zero-asset person.")
                    continue

                should_hide = True
                if args.confirm:
                    try:
                        answer = input("\tHide this person? [y/N] ").strip().casefold()
                    except EOFError:
                        answer = ""
                    should_hide = answer in {"y", "yes"}
                if should_hide:
                    hide_person(base_url, api_key, person_id)
                    person["isHidden"] = True
                    print("\tPerson hidden.")
                else:
                    print("\tSkipped hiding this person.")
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
