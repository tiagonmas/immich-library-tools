#!/usr/bin/env python3
"""Inspect videos for errors and optionally rebuild damaged files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".vob",
    ".webm", ".wmv",
}
WHATSAPP_VIDEO_CODECS = {"h264"}
WHATSAPP_AUDIO_CODECS = {"aac"}
WHATSAPP_PIXEL_FORMATS = {"yuv420p"}
MAX_VIDEO_START_GAP_SECONDS = 1.0


@dataclass
class Inspection:
    path: Path
    probe: dict[str, Any] | None
    probe_error: str | None
    decode_error: str | None
    streams: list[dict[str, Any]]

    @property
    def needs_repair(self) -> bool:
        return self.probe_error is not None or self.decode_error is not None


@dataclass
class CompatibleOutput:
    path: Path
    method: str
    old_source_deleted: bool
    warnings: list[str]


def stream_start_time(stream: dict[str, Any]) -> float | None:
    try:
        return float(stream["start_time"])
    except (KeyError, TypeError, ValueError):
        return None


def video_start_gap(item: Inspection) -> float | None:
    """Return how much later the first video starts than the first audio stream."""
    video_streams = [stream for stream in item.streams if stream.get("codec_type") == "video"]
    if not video_streams:
        return None
    video_start = stream_start_time(video_streams[0])
    if video_start is None:
        return None

    audio_streams = [stream for stream in item.streams if stream.get("codec_type") == "audio"]
    if audio_streams:
        audio_start = stream_start_time(audio_streams[0])
    else:
        audio_start = None
    if audio_start is None:
        format_data = item.probe.get("format", {}) if item.probe else {}
        try:
            audio_start = float(format_data.get("start_time", 0))
        except (TypeError, ValueError):
            audio_start = 0.0
    return max(0.0, video_start - audio_start)


def is_whatsapp_compatible(item: Inspection) -> bool:
    if item.needs_repair or item.path.suffix.lower() != ".mp4":
        return False
    video_streams = [stream for stream in item.streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in item.streams if stream.get("codec_type") == "audio"]
    return (
        bool(video_streams)
        and (video_start_gap(item) or 0) <= MAX_VIDEO_START_GAP_SECONDS
        and all(
            stream.get("codec_name", "").lower() in WHATSAPP_VIDEO_CODECS
            and stream.get("pix_fmt") in WHATSAPP_PIXEL_FORMATS
            for stream in video_streams
        )
        and len(audio_streams) <= 1
        and all(
            stream.get("codec_name", "").lower() in WHATSAPP_AUDIO_CODECS
            for stream in audio_streams
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively inspect video files in a folder with ffprobe and ffmpeg. "
            "Repairs are written to a temporary file and validated before replacing "
            "the original or creating a compatible same-stem file."
        )
    )
    parser.add_argument(
        "--folder", type=Path, default=Path.cwd(),
        help="folder to scan recursively (default: current directory)",
    )
    parser.add_argument("--top", type=positive_int, help="inspect at most X video files")
    parser.add_argument(
        "--compatible", action="store_true",
        help="also check WhatsApp MP4/H.264/AAC compatibility; remux compatible streams or re-encode as needed",
    )
    parser.add_argument(
        "--dry-run", action=argparse.BooleanOptionalAction, default=True,
        help="report planned repairs without changing files (default; use --no-dry-run to repair)",
    )
    parser.add_argument(
        "--no-dryrun", dest="dry_run", action="store_false", help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--confirm", action=argparse.BooleanOptionalAction, default=True,
        help="ask once before replacing files in live mode (default; use --no-confirm to skip)",
    )
    return parser.parse_args()


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe_video(path: Path, ffprobe: str) -> tuple[dict[str, Any] | None, str | None]:
    result = run([
        ffprobe, "-v", "error", "-show_error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        return None, (result.stderr.strip() or f"ffprobe exited with status {result.returncode}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return None, f"ffprobe returned invalid JSON: {error}"
    return parsed, None


def decode_check(path: Path, ffmpeg: str) -> str | None:
    result = run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"])
    if result.returncode == 0 and not result.stderr.strip():
        return None
    details = result.stderr.strip() or f"ffmpeg exited with status {result.returncode}"
    return details


def inspect(path: Path, ffprobe: str, ffmpeg: str) -> Inspection:
    probe, probe_error = probe_video(path, ffprobe)
    streams = probe.get("streams", []) if probe else []
    decode_error = decode_check(path, ffmpeg)
    return Inspection(path, probe, probe_error, decode_error, streams)


def codec_profile(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in {".avi", ".divx"}:
        return "mpeg4", "libmp3lame"
    if suffix == ".webm":
        return "libvpx-vp9", "libopus"
    if suffix in {".mpeg", ".mpg", ".vob"}:
        return "mpeg2video", "mp2"
    if suffix == ".wmv":
        return "wmv2", "wmav2"
    if suffix == ".flv":
        return "flv", "libmp3lame"
    if suffix == ".ogv":
        return "libtheora", "libvorbis"
    # MP4/MOV/M4V and common Matroska/MPEG-TS formats support this profile.
    return "libx264", "aac"


def ffmpeg_rebuild(inspection: Inspection, ffmpeg: str, temp_path: Path) -> None:
    streams = inspection.streams
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video_streams:
        raise RuntimeError("cannot rebuild a file without a readable video stream")

    video_codec, audio_codec = codec_profile(inspection.path)

    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(inspection.path)]
    command += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]
    command += ["-map_metadata", "0", "-map_chapters", "0"]
    command += ["-c:v", video_codec]
    if video_codec == "libx264":
        command += ["-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio_streams:
        command += ["-c:a", audio_codec]
        if audio_codec in {"aac", "libmp3lame"}:
            command += ["-b:a", "192k"]
    command += ["-c:s", "copy", str(temp_path)]
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffmpeg exited with status {result.returncode}")
    if not temp_path.is_file() or temp_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg did not produce a non-empty output file")


def ffmpeg_make_compatible(inspection: Inspection, ffmpeg: str, temp_path: Path) -> None:
    """Create an MP4, stream-copying compatible streams or re-encoding when needed."""
    video_streams = [stream for stream in inspection.streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise RuntimeError("cannot create a compatible video without a readable video stream")
    audio_streams = [stream for stream in inspection.streams if stream.get("codec_type") == "audio"]
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(inspection.path)]
    command += ["-map", "0:v:0", "-map", "0:a:0?"]
    command += ["-map_metadata", "0", "-map_chapters", "0", "-movflags", "+faststart"]
    if can_stream_copy_compatible(inspection):
        command += ["-c", "copy"]
    else:
        start_gap = video_start_gap(inspection)
        if start_gap is not None and start_gap > MAX_VIDEO_START_GAP_SECONDS:
            audio_start = stream_start_time(audio_streams[0]) if audio_streams else None
            if audio_start is None:
                format_data = inspection.probe.get("format", {}) if inspection.probe else {}
                try:
                    audio_start = float(format_data.get("start_time", 0))
                except (TypeError, ValueError):
                    audio_start = 0.0
            command += ["-vf", f"setpts=PTS-STARTPTS+{audio_start:.6f}/TB"]
        command += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p",
        ]
        if audio_streams:
            command += ["-c:a", "aac", "-b:a", "128k"]
    command.append(str(temp_path))
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffmpeg exited with status {result.returncode}")
    if not temp_path.is_file() or temp_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg did not produce a non-empty MP4 file")


def can_stream_copy_compatible(item: Inspection) -> bool:
    if item.needs_repair:
        return False
    video_streams = [stream for stream in item.streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in item.streams if stream.get("codec_type") == "audio"]
    return bool(video_streams) and (video_start_gap(item) or 0) <= MAX_VIDEO_START_GAP_SECONDS and all(
        stream.get("codec_name", "").lower() in WHATSAPP_VIDEO_CODECS
        and stream.get("pix_fmt") in WHATSAPP_PIXEL_FORMATS
        for stream in video_streams
    ) and len(audio_streams) <= 1 and all(
        stream.get("codec_name", "").lower() in WHATSAPP_AUDIO_CODECS
        for stream in audio_streams
    )


def validated_output(temp_path: Path, ffprobe: str, ffmpeg: str) -> None:
    probe, error = probe_video(temp_path, ffprobe)
    if error or not probe or not any(
        stream.get("codec_type") == "video" for stream in probe.get("streams", [])
    ):
        raise RuntimeError(f"rebuilt file failed ffprobe validation: {error or 'no video stream found'}")
    decode_error = decode_check(temp_path, ffmpeg)
    if decode_error:
        raise RuntimeError(f"rebuilt file still has decode errors:\n{decode_error}")


def validated_compatible_output(temp_path: Path, ffprobe: str, ffmpeg: str) -> None:
    probe, error = probe_video(temp_path, ffprobe)
    if error or not probe:
        raise RuntimeError(f"compatible output failed ffprobe validation: {error or 'no probe data'}")
    item = Inspection(temp_path, probe, None, None, probe.get("streams", []))
    if temp_path.suffix.lower() != ".mp4" or not is_whatsapp_compatible(item):
        raise RuntimeError("rebuilt video does not meet the configured WhatsApp compatibility checks")
    decode_error = decode_check(temp_path, ffmpeg)
    if decode_error:
        raise RuntimeError(f"rebuilt video still has decode errors:\n{decode_error}")


def compatible_target(source: Path) -> Path:
    return source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")


def install_no_clobber(source: Path, target: Path) -> None:
    """Install a validated file without replacing an output that appeared meanwhile."""
    try:
        os.link(source, target)
        return
    except FileExistsError:
        raise
    except OSError:
        # Some synced or FUSE-backed libraries do not support hard links.
        created = False
        try:
            with source.open("rb") as source_file, target.open("xb") as target_file:
                created = True
                shutil.copyfileobj(source_file, target_file)
            shutil.copystat(source, target)
        except OSError:
            if created:
                target.unlink(missing_ok=True)
            raise


def xmp_sidecar_migrations(source: Path, target: Path) -> list[tuple[Path, Path]]:
    if source == target:
        return []
    source_specific = source.with_name(source.name + ".xmp")
    source_fallback = source.with_suffix(".xmp")
    target_specific = target.with_name(target.name + ".xmp")
    target_fallback = target.with_suffix(".xmp")
    candidates = [(source_specific, target_specific), (source_fallback, target_fallback)]
    migrations: list[tuple[Path, Path]] = []
    for old_sidecar, new_sidecar in candidates:
        if old_sidecar == new_sidecar or not old_sidecar.is_file():
            continue
        if new_sidecar.exists():
            raise FileExistsError(f"XMP sidecar already exists; refusing to overwrite it: {new_sidecar}")
        migrations.append((old_sidecar, new_sidecar))
    return migrations


def compatible_block_reason(source: Path) -> str | None:
    target = compatible_target(source)
    if target != source and target.exists():
        return f"compatible output already exists; refusing to overwrite it: {target}"
    try:
        xmp_sidecar_migrations(source, target)
    except FileExistsError as error:
        return str(error)
    return None


def write_compatible_output(inspection: Inspection, ffmpeg: str, ffprobe: str) -> CompatibleOutput:
    source = inspection.path
    target = compatible_target(source)
    blocked_reason = compatible_block_reason(source)
    if blocked_reason:
        raise FileExistsError(blocked_reason)
    sidecar_migrations = xmp_sidecar_migrations(source, target)

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.compatible-", suffix=target.suffix, dir=target.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        method = "remuxed" if can_stream_copy_compatible(inspection) else "re-encoded"
        ffmpeg_make_compatible(inspection, ffmpeg, temp_path)
        validated_compatible_output(temp_path, ffprobe, ffmpeg)
        shutil.copystat(source, temp_path)
        if target == source:
            os.replace(temp_path, target)
            return CompatibleOutput(target, method, False, [])
        else:
            # link() gives no-clobber behavior if another file appears after our check.
            install_no_clobber(temp_path, target)
            temp_path.unlink()
            linked_sidecars: list[tuple[Path, Path]] = []
            try:
                for old_sidecar, new_sidecar in sidecar_migrations:
                    install_no_clobber(old_sidecar, new_sidecar)
                    linked_sidecars.append((old_sidecar, new_sidecar))
                source.unlink()
            except OSError:
                target.unlink(missing_ok=True)
                for _, new_sidecar in linked_sidecars:
                    new_sidecar.unlink(missing_ok=True)
                raise

            warnings: list[str] = []
            for old_sidecar, _ in sidecar_migrations:
                try:
                    old_sidecar.unlink()
                except OSError as error:
                    warnings.append(f"could not remove old XMP sidecar {old_sidecar}: {error}")
            return CompatibleOutput(target, method, True, warnings)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def replace_in_place(inspection: Inspection, ffmpeg: str, ffprobe: str) -> None:
    path = inspection.path
    original_stat = path.stat()
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.repair-", suffix=path.suffix, dir=path.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        ffmpeg_rebuild(inspection, ffmpeg, temp_path)
        validated_output(temp_path, ffprobe, ffmpeg)
        os.chmod(temp_path, stat.S_IMODE(original_stat.st_mode))
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def collect_videos(folder: Path, top: int | None) -> list[Path]:
    paths = sorted(
        (path for path in folder.rglob("*")
         if path.is_file() and not path.is_symlink() and path.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda path: str(path).casefold(),
    )
    return paths[:top] if top is not None else paths


def print_inspection(item: Inspection, dry_run: bool, confirm: bool, compatible_mode: bool) -> bool:
    compatible = is_whatsapp_compatible(item) if compatible_mode else False
    compatibility_issue = compatible_mode and not compatible
    if not item.needs_repair and not compatibility_issue:
        if compatible_mode:
            print("  No errors found. WhatsApp-compatible format confirmed.", flush=True)
        else:
            print("  No errors found.", flush=True)
        return False

    if item.needs_repair:
        print("  Errors found:")
        if item.probe_error:
            print("    ffprobe:")
            print(textwrap.indent(item.probe_error, "      "))
        if item.decode_error:
            print("    ffmpeg decode check:")
            print(textwrap.indent(item.decode_error, "      "))
    elif compatibility_issue:
        start_gap = video_start_gap(item)
        if start_gap is not None and start_gap > MAX_VIDEO_START_GAP_SECONDS:
            print(
                "  No decode errors found, but the first video frame starts "
                f"{start_gap:.3f}s after the audio; the conversion will align the video start with the audio."
            )
        else:
            print("  No decode errors found, but the container or codecs are outside the WhatsApp-compatible profile.")

    if compatible_mode:
        target = compatible_target(item.path)
        blocked_reason = compatible_block_reason(item.path)
        if blocked_reason:
            print(f"  Next action: BLOCKED; {blocked_reason}")
            return False
        method = "remux with stream copy (no video/audio re-encoding)" if can_stream_copy_compatible(item) else "re-encode"
        sidecar_migrations = xmp_sidecar_migrations(item.path, target)
        sidecar_action = (
            "migrate the old-name XMP sidecar to the MP4 filename"
            if sidecar_migrations
            else "preserve any shared-stem XMP sidecar"
        )
        if target == item.path:
            plan = (
                f"{method} into a temporary MP4 with H.264 video and AAC audio, validate it, copy metadata, "
                f"then replace the original at the same path ({target})"
            )
        else:
            plan = (
                f"{method} into {target} as an MP4 with H.264 video and AAC audio, validate it, copy "
                f"embedded metadata, chapters, permissions, and timestamps, {sidecar_action}, then "
                f"DELETE the original file ({item.path})"
            )
        print(f"  DESTRUCTIVE ACTION (after validation and confirmation): {plan}.")
    else:
        video_codec, audio_codec = codec_profile(item.path)
        streams = f"video as {video_codec}"
        if any(stream.get("codec_type") == "audio" for stream in item.streams):
            streams += f" and audio as {audio_codec}"
        plan = (
            f"re-encode {streams}, validate the rebuilt file, then replace the original "
            f"at the same path (container: {item.path.suffix.lower()})"
        )

    if dry_run:
        print(f"  Next action: dry-run only; no conversion will run. With --no-dry-run, the script would {plan}.")
    elif confirm:
        print(f"  Next action: after you confirm this file, the script will {plan}.")
    else:
        print(f"  Next action: the script will {plan}.")
    return True


def print_summary(
    *,
    selected: int,
    processed: int,
    files_with_errors: int,
    files_reencoded: int,
    files_remuxed: int,
    old_files_deleted: int,
    new_files_created: int,
    skipped: int,
    blocked: int,
    failed: int,
    dry_run: bool,
    compatible_mode: bool,
    compatibility_confirmed: int = 0,
    compatibility_unconfirmed: int = 0,
    planned_reencodes: int = 0,
    planned_remuxes: int = 0,
) -> None:
    print("\nSummary")
    print(f"  Video files selected: {selected}")
    print(f"  Files processed: {processed}")
    print(f"  Files with probe/decode errors: {files_with_errors}")
    if compatible_mode:
        print(f"  Files meeting the compatibility profile: {compatibility_confirmed}")
        print(f"  Files not confirmed compatible: {compatibility_unconfirmed}")
    print(f"  Files re-encoded: {files_reencoded}")
    print(f"  Files remuxed without re-encoding: {files_remuxed}")
    print(f"  Original files deleted after conversion: {old_files_deleted}")
    if dry_run:
        print(f"  Files that would be re-encoded: {planned_reencodes}")
        print(f"  Files that would be remuxed: {planned_remuxes}")
        print("  Files changed: 0 (dry-run)")
    if new_files_created:
        print(f"  New files created with a compatible extension: {new_files_created}")
    if skipped:
        print(f"  Files skipped: {skipped}")
    if blocked:
        print(f"  Files blocked by an existing output: {blocked}")
    if failed:
        print(f"  Failed conversions: {failed}")


def main() -> int:
    args = parse_args()
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Error: not a directory: {folder}", file=sys.stderr)
        return 2
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("Error: both ffmpeg and ffprobe must be installed and on PATH.", file=sys.stderr)
        return 2

    videos = collect_videos(folder, args.top)
    if not videos:
        print(f"No video files found in {folder}")
        print_summary(
            selected=0, processed=0, files_with_errors=0, files_reencoded=0,
            files_remuxed=0, old_files_deleted=0,
            new_files_created=0, skipped=0, blocked=0, failed=0,
            dry_run=args.dry_run, compatible_mode=args.compatible,
        )
        return 0
    print(f"Inspecting {len(videos)} video file(s) under {folder}.", flush=True)
    if args.compatible:
        print(
            "Compatibility target: MP4 with H.264/yuv420p video and AAC or no audio. "
            "WhatsApp size and resolution limits vary and are not enforced here.",
            flush=True,
        )
    confirm_remaining = args.confirm
    if not args.dry_run and confirm_remaining:
        print(
            "The script will ask before each file that needs action; enter 'a' to approve it and all remaining files without more prompts.",
            flush=True,
        )

    repair_candidates = 0
    processed = 0
    files_with_errors = 0
    compatibility_confirmed = 0
    compatibility_unconfirmed = 0
    rebuilt_count = 0
    remuxed_count = 0
    old_files_deleted = 0
    new_files_created = 0
    failures = 0
    skipped = 0
    blocked = 0
    planned_reencodes = 0
    planned_remuxes = 0
    for index, path in enumerate(videos, start=1):
        print(f"\n[{index}/{len(videos)}] Checking: {path}", flush=True)
        item = inspect(path, ffprobe, ffmpeg)
        processed += 1
        if item.needs_repair:
            files_with_errors += 1
        item_is_compatible = args.compatible and is_whatsapp_compatible(item)
        if args.compatible:
            if item_is_compatible:
                compatibility_confirmed += 1
            else:
                compatibility_unconfirmed += 1
        needs_action = print_inspection(item, args.dry_run, confirm_remaining, args.compatible)
        sys.stdout.flush()
        if not needs_action:
            if (
                args.compatible
                and not item_is_compatible
                and compatible_block_reason(item.path)
            ):
                blocked += 1
            continue
        repair_candidates += 1
        if args.dry_run:
            if args.compatible and can_stream_copy_compatible(item):
                planned_remuxes += 1
            else:
                planned_reencodes += 1
        if args.dry_run:
            continue
        if confirm_remaining:
            if not sys.stdin.isatty():
                print("  Skipped: cannot ask for confirmation in a non-interactive session.")
                skipped += 1
                continue
            try:
                answer = input(
                    "  Proceed with this action? [y]es / [n]o / [a]ll remaining: "
                )
            except EOFError:
                answer = ""
            choice = answer.strip().lower()
            if choice in {"a", "all"}:
                confirm_remaining = False
            elif choice not in {"y", "yes"}:
                print("  Skipped: no confirmation given.")
                skipped += 1
                continue
        try:
            if args.compatible:
                output = write_compatible_output(item, ffmpeg, ffprobe)
                if output.method == "remuxed":
                    remuxed_count += 1
                else:
                    rebuilt_count += 1
                if output.old_source_deleted:
                    old_files_deleted += 1
                    print(f"  Created and validated {output.path}; deleted the original at: {item.path}")
                else:
                    print(f"  Created and validated {output.path}; replaced the original at that path.")
                for warning in output.warnings:
                    print(f"  Metadata cleanup warning: {warning}")
                if output.old_source_deleted:
                    new_files_created += 1
            else:
                replace_in_place(item, ffmpeg, ffprobe)
                print(f"  Rebuilt and validated; replaced the original at: {item.path}")
                rebuilt_count += 1
        except (OSError, RuntimeError) as error:
            failures += 1
            print("  Repair failed:")
            print(textwrap.indent(str(error), "    "))

    print_summary(
        selected=len(videos),
        processed=processed,
        files_with_errors=files_with_errors,
        files_reencoded=rebuilt_count,
        files_remuxed=remuxed_count,
        old_files_deleted=old_files_deleted,
        new_files_created=new_files_created,
        skipped=skipped,
        blocked=blocked,
        failed=failures,
        dry_run=args.dry_run,
        compatible_mode=args.compatible,
        compatibility_confirmed=compatibility_confirmed,
        compatibility_unconfirmed=compatibility_unconfirmed,
        planned_reencodes=planned_reencodes,
        planned_remuxes=planned_remuxes,
    )
    if skipped and args.confirm and not sys.stdin.isatty():
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
