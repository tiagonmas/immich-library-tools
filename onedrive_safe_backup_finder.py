#!/usr/bin/env python3
"""Compare OneDrive safeBackup files with their likely originals."""

from __future__ import annotations

import argparse
import difflib
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SAFE_BACKUP_MARKER = re.compile(r"-safeBackup-(?P<number>\d+)$", re.IGNORECASE)
IGNORED_METADATA_NAMES = {
    "FileAccessDate",
    "FileInodeChangeDate",
    "FileModifyDate",
    "FileName",
    "FilePermissions",
    "FileSize",
    "Directory",
}
JPEG_METADATA_MARKERS = {0xE1, 0xED, 0xFE}  # EXIF/XMP, IPTC, and comments.
IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
    ".png", ".tif", ".tiff", ".webp",
}
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


@dataclass
class BackupName:
    number: str
    prefix: str


@dataclass
class FileComparison:
    original: Path
    backup: Path
    metadata_error: str | None
    original_metadata: dict[str, Any]
    backup_metadata: dict[str, Any]
    original_hash: str | None
    backup_hash: str | None
    original_size: int | None
    backup_size: int | None
    image_content_identical: bool | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find OneDrive safeBackup files, compare them with likely originals, "
            "and optionally delete backup files and their XMP sidecars."
        )
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=Path.cwd(),
        metavar="PATH",
        help="folder to scan recursively (default: current directory)",
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="process at most N safeBackup files (default: all)",
    )
    parser.add_argument(
        "--show-metadata",
        action="store_true",
        help="show differences in metadata and XMP sidecars",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="report planned deletions without deleting (default; use --no-dry-run to delete)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help=(
            "auto-delete when image content is verified identical; still ask if "
            "image content differs or cannot be verified"
        ),
    )
    args = parser.parse_args()
    if args.top is not None and args.top < 0:
        parser.error("--top must be zero or greater")
    return args


def backup_name(path: Path) -> BackupName | None:
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    match = SAFE_BACKUP_MARKER.search(stem)
    if not match:
        return None
    return BackupName(number=match.group("number"), prefix=stem[: match.start()])


def iter_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    for current, directories, filenames in os.walk(folder, followlinks=False):
        directories.sort()
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_file() and backup_name(path) is not None:
                found.append(path)
    return found


def original_candidates(backup: Path) -> list[Path]:
    parsed = backup_name(backup)
    if parsed is None:
        return []

    # A backup may be named "photo-host-safeBackup-0001.jpg" or, in older or
    # customized naming, "photo-safeBackup-0001.jpg". Try each possible
    # hyphen boundary and only accept originals that actually exist nearby.
    candidate_stems = {parsed.prefix}
    candidate_stems.update(
        parsed.prefix[:index]
        for index, character in enumerate(parsed.prefix)
        if character == "-" and index > 0
    )
    siblings: dict[str, list[Path]] = {}
    for child in backup.parent.iterdir():
        if child.is_file() and child != backup:
            siblings.setdefault(child.name.casefold(), []).append(child)
    candidates: dict[str, Path] = {}
    for stem in candidate_stems:
        name = f"{stem}{backup.suffix}"
        for candidate in siblings.get(name.casefold(), []):
            candidates[str(candidate)] = candidate
    return sorted(candidates.values(), key=lambda candidate: candidate.name.casefold())


def associated_xmps(media_path: Path) -> list[Path]:
    """Return common ExifTool/Immich sidecar names for this media file."""
    possible = [
        Path(f"{media_path}.xmp"),
        Path(f"{media_path}.XMP"),
        media_path.with_suffix(".xmp"),
        media_path.with_suffix(".XMP"),
    ]
    return list(dict.fromkeys(path for path in possible if path.is_file()))


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def jpeg_content_digest(path: Path) -> tuple[str, int] | None:
    """Hash JPEG markers and scan data, omitting common metadata segments."""
    digest = hashlib.sha256()
    included_size = 0

    def add(data: bytes) -> None:
        nonlocal included_size
        digest.update(data)
        included_size += len(data)

    try:
        with path.open("rb") as source:
            if source.read(2) != b"\xff\xd8":
                return None
            add(b"\xff\xd8")
            pending_marker: tuple[int, bytes] | None = None

            while True:
                if pending_marker is None:
                    prefix = source.read(1)
                    if not prefix:
                        return None
                    if prefix != b"\xff":
                        return None
                    marker_bytes = bytearray(prefix)
                    marker = source.read(1)
                    while marker == b"\xff":
                        marker_bytes.extend(marker)
                        marker = source.read(1)
                    if not marker:
                        return None
                    marker_bytes.extend(marker)
                    marker_id = marker[0]
                else:
                    marker_id, marker_bytes = pending_marker
                    pending_marker = None

                if marker_id == 0xD9:
                    add(bytes(marker_bytes))
                    break
                if marker_id in {0xD8, 0x01, *range(0xD0, 0xD8)}:
                    add(bytes(marker_bytes))
                    continue

                segment_length_bytes = source.read(2)
                if len(segment_length_bytes) != 2:
                    return None
                segment_length = int.from_bytes(segment_length_bytes, "big")
                if segment_length < 2:
                    return None
                segment_data = source.read(segment_length - 2)
                if len(segment_data) != segment_length - 2:
                    return None

                if marker_id not in JPEG_METADATA_MARKERS:
                    add(bytes(marker_bytes) + segment_length_bytes + segment_data)

                if marker_id != 0xDA:  # SOS: next bytes are compressed scan data.
                    continue

                while True:
                    byte = source.read(1)
                    if not byte:
                        return None
                    if byte != b"\xff":
                        add(byte)
                        continue

                    marker_bytes = bytearray(byte)
                    next_byte = source.read(1)
                    while next_byte == b"\xff":
                        marker_bytes.extend(next_byte)
                        next_byte = source.read(1)
                    if not next_byte:
                        return None
                    marker_bytes.extend(next_byte)
                    marker_id = next_byte[0]
                    if marker_id == 0x00 or 0xD0 <= marker_id <= 0xD7:
                        add(bytes(marker_bytes))
                        continue
                    pending_marker = (marker_id, bytes(marker_bytes))
                    break

    except OSError:
        return None

    return digest.hexdigest(), included_size


def exiftool_metadata(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["exiftool", "-json", "-G1", "-a", "-struct", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not run ExifTool: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail or f"ExifTool exited with {completed.returncode}")
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ExifTool returned invalid JSON: {error}") from error
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise RuntimeError("ExifTool returned an unexpected JSON response")

    metadata = records[0]
    metadata.pop("SourceFile", None)
    return {
        key: value
        for key, value in metadata.items()
        if key.rsplit(":", 1)[-1] not in IGNORED_METADATA_NAMES
    }


def metadata_value_lines(value: Any) -> list[str]:
    if value == "<absent>":
        rendered = '"<absent>"'
    else:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return rendered.splitlines()


def changed_metadata_values(
    original: Any, backup: Any, path: str = "$"
) -> list[tuple[str, Any, Any]]:
    if isinstance(original, dict) and isinstance(backup, dict):
        differences = []
        for key in sorted(set(original) | set(backup), key=str):
            child_path = f"{path}[{json.dumps(str(key), ensure_ascii=False)}]"
            old_value = original.get(key, "<absent>")
            new_value = backup.get(key, "<absent>")
            differences.extend(
                changed_metadata_values(old_value, new_value, child_path)
            )
        return differences

    if isinstance(original, list) and isinstance(backup, list):
        differences = []
        for index in range(max(len(original), len(backup))):
            old_value = original[index] if index < len(original) else "<absent>"
            new_value = backup[index] if index < len(backup) else "<absent>"
            differences.extend(
                changed_metadata_values(old_value, new_value, f"{path}[{index}]")
            )
        return differences

    if original == backup:
        return []
    return [(path, original, backup)]


def compact_metadata_value(value: Any) -> str:
    if value == "<absent>":
        return "<absent>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def print_value_diff(original: Any, backup: Any) -> None:
    print("\t\t  Changed values:")
    for path, old_value, new_value in changed_metadata_values(original, backup):
        print(f"\t\t    {path}")
        print(f"\t\t      original: {compact_metadata_value(old_value)}")
        print(f"\t\t      backup:   {compact_metadata_value(new_value)}")

    diff_lines = difflib.unified_diff(
        metadata_value_lines(original),
        metadata_value_lines(backup),
        fromfile="original",
        tofile="backup",
        n=0,
        lineterm="",
    )
    print("\t\t  Difference:")
    for line in diff_lines:
        print(f"\t\t    {line}")


def metadata_differences(
    original: dict[str, Any], backup: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    differences: list[tuple[str, Any, Any]] = []
    for key in sorted(set(original) | set(backup), key=str.casefold):
        original_value = original.get(key, "<absent>")
        backup_value = backup.get(key, "<absent>")
        if original_value != backup_value:
            differences.append((key, original_value, backup_value))
    return differences


def image_dimensions(metadata: dict[str, Any]) -> tuple[int, int] | None:
    values: dict[str, Any] = {}
    for key, value in metadata.items():
        name = key.rsplit(":", 1)[-1]
        if name in {"ImageWidth", "ImageHeight"} and isinstance(value, (int, float)):
            values[name] = int(value)
    width, height = values.get("ImageWidth"), values.get("ImageHeight")
    if width and height:
        return width, height
    return None


@functools.lru_cache(maxsize=1)
def ffmpeg_is_usable() -> bool:
    executable = shutil.which("ffmpeg")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "-version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def decode_first_frame(path: Path, directory: Path, label: str) -> Path | None:
    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    output = directory / f"{label}.rgb"
    try:
        result = subprocess.run(
            [
                executable,
                "-v", "error",
                "-nostdin",
                "-noautorotate",
                "-i", str(path),
                "-frames:v", "1",
                "-f", "rawvideo",
                "-pix_fmt", "rgba",
                "-y", str(output),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not output.is_file():
        return None
    return output


def compare_rgb_files(
    original: Path, backup: Path, width: int, height: int
) -> tuple[bool, int, float, tuple[int, int, int, int] | None] | None:
    channels = 4
    expected_size = width * height * channels
    try:
        if original.stat().st_size != expected_size or backup.stat().st_size != expected_size:
            return None
        changed_pixels = 0
        total_channel_difference = 0
        min_x, min_y, max_x, max_y = width, height, -1, -1
        with original.open("rb") as first, backup.open("rb") as second:
            for y in range(height):
                row_a = first.read(width * channels)
                row_b = second.read(width * channels)
                if len(row_a) != width * channels or len(row_b) != width * channels:
                    return None
                for x in range(width):
                    offset = x * channels
                    a = row_a[offset : offset + channels]
                    b = row_b[offset : offset + channels]
                    if a != b:
                        changed_pixels += 1
                        min_x, min_y = min(min_x, x), min(min_y, y)
                        max_x, max_y = max(max_x, x), max(max_y, y)
                        total_channel_difference += sum(abs(c1 - c2) for c1, c2 in zip(a, b))
        bounds = None if changed_pixels == 0 else (min_x, min_y, max_x, max_y)
        total_pixels = width * height
        mean_difference = total_channel_difference / (total_pixels * channels)
        return changed_pixels == 0, changed_pixels, mean_difference, bounds
    except OSError:
        return None


def compare_image_content(
    original_path: Path,
    backup_path: Path,
    original_metadata: dict[str, Any],
    backup_metadata: dict[str, Any],
    original_file_hash: str,
    backup_file_hash: str,
    ffmpeg_available: bool,
) -> bool | None:
    print("\tImage content:")
    jpeg_stream_identical: bool | None = None
    if original_path.suffix.casefold() in {".jpg", ".jpeg"}:
        original_content = jpeg_content_digest(original_path)
        backup_content = jpeg_content_digest(backup_path)
        if original_content is None or backup_content is None:
            message = "JPEG content stream (excluding common metadata segments): comparison unavailable."
            print(f"\t\t{RED}{message}{RESET}")
        elif original_content[0] == backup_content[0]:
            jpeg_stream_identical = True
            message = "JPEG content stream (excluding common metadata segments): identical."
            print(f"\t\t{GREEN}{message}{RESET}")
        else:
            jpeg_stream_identical = False
            message = "JPEG content stream (excluding common metadata segments): different."
            print(f"\t\t{RED}{message}{RESET}")

    if original_path.suffix.casefold() not in IMAGE_EXTENSIONS:
        print("\t\tPixel comparison not applicable to this file type.")
        return jpeg_stream_identical

    if not ffmpeg_available:
        print("\t\tPixel comparison unavailable (install/configure ffmpeg for a pixel-level diff).")
        if original_file_hash == backup_file_hash:
            print("\t\tWhole-file comparison: identical.")
        else:
            print("\t\tWhole-file comparison: different (this can be due to metadata or image data).")
        return jpeg_stream_identical

    original_dimensions = image_dimensions(original_metadata)
    backup_dimensions = image_dimensions(backup_metadata)
    if original_dimensions is None or backup_dimensions is None:
        print("\t\tPixel comparison unavailable: could not read image dimensions.")
        return jpeg_stream_identical
    if original_dimensions != backup_dimensions:
        print(
            f"\t\tPixel comparison: dimensions differ "
            f"({original_dimensions[0]}x{original_dimensions[1]} vs "
            f"{backup_dimensions[0]}x{backup_dimensions[1]})."
        )
        return jpeg_stream_identical if jpeg_stream_identical is not None else False

    with tempfile.TemporaryDirectory(prefix="safe-backup-compare-") as temporary:
        directory = Path(temporary)
        original_rgb = decode_first_frame(original_path, directory, "original")
        backup_rgb = decode_first_frame(backup_path, directory, "backup")
        if original_rgb is None or backup_rgb is None:
            print("\t\tPixel comparison failed: ffmpeg could not decode one of the files.")
            return jpeg_stream_identical
        result = compare_rgb_files(
            original_rgb,
            backup_rgb,
            original_dimensions[0],
            original_dimensions[1],
        )
    if result is None:
        print("\t\tPixel comparison failed: decoded frame size did not match metadata dimensions.")
        pixel_content_identical = None
    elif result[0]:
        print(
            f"\t\tFirst-frame pixels: identical ({original_dimensions[0]}x"
            f"{original_dimensions[1]})."
        )
        pixel_content_identical = True
    else:
        bounds = result[3]
        print(
            f"\t\tFirst-frame pixels: {result[1]} of "
            f"{original_dimensions[0] * original_dimensions[1]} differ; "
            f"mean absolute channel difference {result[2]:.3f}/255."
        )
        if bounds is not None:
            print(f"\t\tChanged-pixel bounds: x={bounds[0]}..{bounds[2]}, y={bounds[1]}..{bounds[3]}.")
        pixel_content_identical = False
    if jpeg_stream_identical is not None:
        return jpeg_stream_identical
    return pixel_content_identical


def print_metadata_differences(
    original_metadata: dict[str, Any], backup_metadata: dict[str, Any]
) -> None:
    differences = metadata_differences(original_metadata, backup_metadata)
    if not differences:
        print("\tMetadata: no differences found (file-system timestamps and sizes excluded).")
        return
    print(f"\tMetadata differences: {len(differences)}")
    for key, original_value, backup_value in differences:
        print(f"\t\t{key}")
        print_value_diff(original_value, backup_value)


def compare_files(
    original: Path, backup: Path, label: str, show_metadata: bool = False
) -> FileComparison:
    print(f"\t{label}: {backup.name} vs {original.name}")
    metadata_error: str | None = None
    original_metadata: dict[str, Any] = {}
    backup_metadata: dict[str, Any] = {}
    image_content_identical: bool | None = None
    try:
        original_metadata = exiftool_metadata(original)
        backup_metadata = exiftool_metadata(backup)
        if show_metadata:
            print_metadata_differences(original_metadata, backup_metadata)
    except RuntimeError as error:
        metadata_error = str(error)
        if show_metadata:
            print(f"\tMetadata comparison error: {metadata_error}")

    try:
        original_hash, original_size = sha256_file(original)
        backup_hash, backup_size = sha256_file(backup)
    except OSError as error:
        print(f"\tFile comparison error: {error}")
        original_hash = backup_hash = None
        original_size = backup_size = None

    if original_hash is not None and backup_hash is not None:
        status = "identical" if original_hash == backup_hash else "different"
        print(
            f"\tFile comparison: {status} "
            f"(original {original_size} bytes; backup {backup_size} bytes)."
        )
        if original.suffix.casefold() in IMAGE_EXTENSIONS:
            image_content_identical = compare_image_content(
                original,
                backup,
                original_metadata,
                backup_metadata,
                original_hash,
                backup_hash,
                ffmpeg_is_usable(),
            )
    return FileComparison(
        original=original,
        backup=backup,
        metadata_error=metadata_error,
        original_metadata=original_metadata,
        backup_metadata=backup_metadata,
        original_hash=original_hash,
        backup_hash=backup_hash,
        original_size=original_size,
        backup_size=backup_size,
        image_content_identical=image_content_identical,
    )


def print_xmp_comparisons(
    original: Path, backup: Path, show_metadata: bool = False
) -> tuple[list[Path], bool, bool]:
    original_xmps = associated_xmps(original)
    backup_xmps = associated_xmps(backup)
    if not original_xmps and not backup_xmps:
        print("\tXMP sidecars: none found.")
        return [], False, False

    print("\tXMP sidecars:")
    original_by_suffix = {path.name[len(original.name) :].casefold(): path for path in original_xmps if path.name.casefold().startswith(original.name.casefold())}
    backup_by_suffix = {path.name[len(backup.name) :].casefold(): path for path in backup_xmps if path.name.casefold().startswith(backup.name.casefold())}

    # Also support the shorter basename.xmp spelling, where the media extension
    # is replaced by .xmp rather than appended before it.
    for path in original_xmps:
        if path not in original_by_suffix.values():
            original_by_suffix["short:" + path.suffix.casefold()] = path
    for path in backup_xmps:
        if path not in backup_by_suffix.values():
            backup_by_suffix["short:" + path.suffix.casefold()] = path

    deletable: list[Path] = []
    comparison_incomplete = False
    sidecar_files_differ = False
    for suffix in sorted(set(original_by_suffix) | set(backup_by_suffix)):
        original_sidecar = original_by_suffix.get(suffix)
        backup_sidecar = backup_by_suffix.get(suffix)
        if backup_sidecar is None:
            assert original_sidecar is not None
            print(f"\t\tOriginal-only sidecar: {original_sidecar.name}")
            sidecar_files_differ = True
            continue
        deletable.append(backup_sidecar)
        if original_sidecar is None:
            print(f"\t\tBackup-only sidecar: {backup_sidecar.name}")
            sidecar_files_differ = True
            continue
        result = compare_files(
            original_sidecar,
            backup_sidecar,
            "XMP comparison",
            show_metadata=show_metadata,
        )
        if result.original_hash != result.backup_hash:
            sidecar_files_differ = True
        if result.metadata_error or result.original_hash is None or result.backup_hash is None:
            comparison_incomplete = True
    return sorted(set(deletable)), comparison_incomplete, sidecar_files_differ


def process_backup(
    backup: Path, dry_run: bool, confirm: bool, show_metadata: bool
) -> None:
    print("\n" + "-" * 72)
    print(f"Safe backup: {backup}")
    candidates = original_candidates(backup)
    if not candidates:
        message = "No matching original file found; leaving this backup untouched."
        print(f"\t{RED}{message}{RESET}")
        return
    if len(candidates) > 1:
        message = "Ambiguous original match; leaving this backup untouched."
        print(f"\t{RED}{message}{RESET}")
        for candidate in candidates:
            print(f"\t\tPossible original: {candidate.name}")
        return

    original = candidates[0]
    print(f"\tOriginal: {original}")
    comparison = compare_files(
        original, backup, "Image/file comparison", show_metadata=show_metadata
    )
    backup_xmps, xmp_comparison_incomplete, xmp_sidecars_differ = print_xmp_comparisons(
        original, backup, show_metadata=show_metadata
    )
    if (
        comparison.metadata_error
        or comparison.original_hash is None
        or comparison.backup_hash is None
        or xmp_comparison_incomplete
    ):
        message = "Comparison was incomplete; will not delete this backup."
        print(f"\t{RED}{message}{RESET}")
        return

    file_bytes_identical = comparison.original_hash == comparison.backup_hash
    image_content_verified_same = (
        file_bytes_identical or comparison.image_content_identical is True
    )
    if (
        image_content_verified_same
        and (not file_bytes_identical or xmp_sidecars_differ)
    ):
        message = "Image content is identical; observed differences are only in metadata or XMP sidecars."
        print(f"\t{GREEN}{message}{RESET}")

    files_to_delete = [backup, *backup_xmps]
    if dry_run:
        print("\tDry run: would delete the safe backup and the listed backup XMP sidecar(s).")
        for path in files_to_delete:
            print(f"\t\tWould delete: {path.name}")
        if not confirm and not image_content_verified_same:
            print(
                "\tImage content differs or could not be verified; live mode will still ask for confirmation."
            )
        return

    should_delete = True
    must_confirm = confirm or not image_content_verified_same
    if must_confirm:
        if not confirm and not image_content_verified_same:
            print(
                "\tImage content differs or could not be verified; confirmation is required even with --no-confirm."
            )
        try:
            answer = input("\tDelete this safe backup and its XMP sidecar(s)? [y/N] ").strip().casefold()
        except EOFError:
            answer = ""
        should_delete = answer in {"y", "yes"}
    if not should_delete:
        print("\tKept the safe backup and its XMP sidecar(s).")
        return

    for path in files_to_delete:
        try:
            path.unlink()
            print(f"\tDeleted: {path.name}")
        except OSError as error:
            print(f"\tCould not delete {path.name}: {error}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Error: folder does not exist or is not a directory: {folder}", file=sys.stderr)
        return 2
    if shutil.which("exiftool") is None:
        print("Error: ExifTool is required; install it and ensure it is on PATH.", file=sys.stderr)
        return 2

    backups = iter_files(folder)
    selected = backups if args.top is None else backups[: args.top]
    mode = "DRY RUN: no files will be deleted" if args.dry_run else "LIVE: matching backup files can be deleted"
    if not args.dry_run and args.no_confirm:
        mode += "; identical/metadata-only files auto-confirmed, changed or unverified image content still prompts"
    print(f"Folder: {folder}")
    print(f"Found {len(backups)} safeBackup files; processing {len(selected)}.")
    print(f"Mode: {mode}")
    if args.no_confirm and args.dry_run:
        print("Warning: --no-confirm has no effect in dry-run mode.")

    for backup in selected:
        process_backup(
            backup,
            args.dry_run,
            confirm=not args.no_confirm,
            show_metadata=args.show_metadata,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
