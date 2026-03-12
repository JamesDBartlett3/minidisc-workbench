#!/usr/bin/env python3
"""
Resize Album to Fit MiniDisc
-----------------------------
Resizes (speeds up) album tracks proportionally so the total playtime
fits on a MiniDisc.  Supports two modes:

  • Speed       – Increases playback speed (pitch also rises).
  • TimeStretch – Increases speed without changing pitch
                  (requires ffmpeg built with librubberband).

Standard MiniDiscs hold 74 minutes in SP mode.  Use --lp2 to double
capacity (2× TargetMinutes) or --lp4 to quadruple it (4× TargetMinutes),
matching MDLP Long Play recording modes.

Output is lossless FLAC by default.  Other lossless formats:
  WAV, AIFF, ALAC (Apple Lossless), APE (Monkey's Audio), WavPack, TTA.

Requirements:
  ffmpeg and ffprobe must be installed and available in PATH.

Usage:
  python resize_album_to_fit_md.py --input-folder ./MyAlbum --mode Speed
  python resize_album_to_fit_md.py --input-folder ./MyAlbum --lp2 --mode TimeStretch
  python resize_album_to_fit_md.py --input-folder ./MyAlbum --lp4 --mode Speed --format ALAC
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".m4a", ".ogg",
    ".opus", ".wma", ".aac", ".aiff",
}

OUTPUT_FORMATS = {
    "FLAC":    ("flac",  ["-c:a", "flac", "-compression_level", "8"]),
    "WAV":     ("wav",   ["-c:a", "pcm_s16le"]),
    "AIFF":    ("aiff",  ["-c:a", "pcm_s16be"]),
    "ALAC":    ("m4a",   ["-c:a", "alac"]),
    "APE":     ("ape",   ["-c:a", "ape", "-compression_level", "3000"]),
    "WAVPACK": ("wv",    ["-c:a", "wavpack", "-compression_level", "8"]),
    "TTA":     ("tta",   ["-c:a", "tta"]),
}

MODE_MULTIPLIERS = {"SP": 1, "LP2": 2, "LP4": 4}

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def format_duration_short(seconds: float) -> str:
    """Format *seconds* as ``M:SS`` (total minutes).

    >>> format_duration_short(74 * 60)
    '74:00'
    >>> format_duration_short(3661)
    '61:01'
    """
    if seconds < 0:
        seconds = 0.0
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def format_duration_long(seconds: float) -> str:
    """Format *seconds* as ``M:SS.mmm`` (total minutes, with milliseconds).

    >>> format_duration_long(74 * 60 + 1.5)
    '74:01.500'
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}.{ms:03d}"


def check_tool(name: str) -> bool:
    """Return True if *name* is found in PATH."""
    return shutil.which(name) is not None


def get_audio_duration(path: str | Path) -> float:
    """Return the duration of *path* in seconds using ffprobe.

    Raises ``RuntimeError`` if ffprobe cannot determine the duration.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError(f"Unable to get duration from ffprobe for '{path}'.")
    return float(raw)


def build_atempo_chain(speed_factor: float) -> str:
    """Build an ffmpeg ``atempo`` filter string for *speed_factor*.

    The ``atempo`` filter only supports values in [0.5, 2.0].  For factors
    outside this range the filter is chained (e.g. ``atempo=2.0,atempo=1.25``).

    >>> build_atempo_chain(1.5)
    'atempo=1.5'
    >>> build_atempo_chain(3.0)
    'atempo=2.0,atempo=1.5'
    >>> build_atempo_chain(0.25)
    'atempo=0.5,atempo=0.5'
    """
    tempo = speed_factor
    chain: List[str] = []

    while tempo > 2.0:
        chain.append("atempo=2.0")
        tempo /= 2.0
    while tempo < 0.5:
        chain.append("atempo=0.5")
        tempo /= 0.5

    # Round to 6 decimal places to avoid floating-point noise in the filter string
    chain.append(f"atempo={round(tempo, 6)}")
    return ",".join(chain)


def calculate_speed_factor(
    total_seconds: float,
    target_minutes: float,
    lp_mode: str = "SP",
) -> float:
    """Return the speed factor required to fit *total_seconds* into *target_minutes*.

    A factor > 1 means faster playback (shorter duration).

    >>> abs(calculate_speed_factor(90, 1) - 1.5) < 1e-9
    True
    >>> abs(calculate_speed_factor(90, 0.5, 'LP2') - 1.5) < 1e-9
    True
    """
    multiplier = MODE_MULTIPLIERS.get(lp_mode.upper(), 1)
    effective_minutes = target_minutes * multiplier
    target_seconds = effective_minutes * 60.0
    if target_seconds <= 0:
        raise ValueError("Target duration must be positive.")
    return total_seconds / target_seconds


def find_audio_files(
    folder: str | Path,
    file_pattern: Optional[str] = None,
) -> List[Path]:
    """Return sorted audio files in *folder* matching *file_pattern* (or all audio extensions)."""
    folder = Path(folder)
    if file_pattern:
        files = sorted(folder.glob(file_pattern))
    else:
        files = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
    return [f for f in files if f.is_file()]


def resize_tracks(
    input_files: Sequence[str | Path],
    output_folder: str | Path,
    speed_factor: float,
    mode: str = "Speed",
    output_format: str = "FLAC",
    *,
    quiet: bool = False,
) -> List[Tuple[str, Optional[float]]]:
    """Resize each track in *input_files* by *speed_factor* using ffmpeg.

    Parameters
    ----------
    input_files:    Sequence of audio file paths to process.
    output_folder:  Directory where processed files will be written.
    speed_factor:   Factor > 1 speeds up (shortens) the track.
    mode:           ``"Speed"`` (atempo, pitch changes) or
                    ``"TimeStretch"`` (rubberband, pitch preserved).
    output_format:  One of FLAC / WAV / AIFF / ALAC / APE / WavPack / TTA.
    quiet:          Suppress per-track progress messages.

    Returns a list of ``(output_path, actual_duration_seconds)`` tuples.
    ``actual_duration_seconds`` is ``None`` if the file could not be read back.
    """
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    fmt_upper = output_format.upper()
    if fmt_upper not in OUTPUT_FORMATS:
        raise ValueError(
            f"Unsupported output format '{output_format}'. "
            f"Choose from: {', '.join(OUTPUT_FORMATS)}."
        )
    out_ext, codec_args = OUTPUT_FORMATS[fmt_upper]

    mode_lower = mode.lower()
    if mode_lower == "speed":
        audio_filter = build_atempo_chain(speed_factor)
    elif mode_lower == "timestretch":
        audio_filter = f"rubberband=tempo={round(speed_factor, 6)}"
    else:
        raise ValueError(
            f"Unsupported mode '{mode}'. Choose 'Speed' or 'TimeStretch'."
        )

    results: List[Tuple[str, Optional[float]]] = []
    total = len(input_files)

    for idx, src in enumerate(input_files, start=1):
        src = Path(src)
        stem = src.stem
        out_path = output_folder / f"{stem}.{out_ext}"

        if not quiet:
            try:
                orig_dur = get_audio_duration(src)
                new_dur = orig_dur / speed_factor
                print(
                    f"[{idx}/{total}] Processing: {src.name}\n"
                    f"         Original: {format_duration_short(orig_dur)}"
                    f" -> New: {format_duration_short(new_dur)}"
                )
            except RuntimeError:
                print(f"[{idx}/{total}] Processing: {src.name}")

        ffmpeg_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-map", "0:a",
            "-af", audio_filter,
        ] + codec_args + [
            "-map", "0:v?",
            "-c:v", "copy",
            str(out_path),
        ]

        proc = subprocess.run(ffmpeg_args, capture_output=True, text=True)

        if proc.returncode != 0:
            if not quiet:
                print(
                    f"  Warning: ffmpeg reported an issue processing '{src.name}'.",
                    file=sys.stderr,
                )
                if mode_lower == "timestretch":
                    print(
                        "  Note: TimeStretch mode requires ffmpeg built with librubberband support.\n"
                        "  If not available, try --mode Speed instead.",
                        file=sys.stderr,
                    )
            results.append((str(out_path), None))
            continue

        # Verify the output and read back its actual duration
        if out_path.is_file():
            try:
                actual_dur = get_audio_duration(out_path)
            except RuntimeError:
                actual_dur = None
            results.append((str(out_path), actual_dur))
        else:
            results.append((str(out_path), None))

    return results


def resize_album(
    input_folder: str | Path,
    output_folder: str | Path,
    target_minutes: float = 74.0,
    lp_mode: str = "SP",
    mode: str = "Speed",
    output_format: str = "FLAC",
    file_pattern: Optional[str] = None,
    *,
    yes: bool = False,
    quiet: bool = False,
) -> int:
    """High-level entry point: resize an entire album folder to fit a MiniDisc.

    Parameters
    ----------
    input_folder:   Folder containing the source audio tracks.
    output_folder:  Folder where resized tracks will be saved.
    target_minutes: Base disc capacity in minutes (default 74).
    lp_mode:        ``"SP"`` / ``"LP2"`` / ``"LP4"`` (multiplies target).
    mode:           ``"Speed"`` or ``"TimeStretch"``.
    output_format:  Output codec/container (FLAC, WAV, AIFF, ALAC, APE, WavPack, TTA).
    file_pattern:   Optional glob pattern to filter input files.
    yes:            Skip the interactive confirmation prompt.
    quiet:          Suppress progress output.

    Returns the shell exit code (0 = success, 1 = error).
    """
    input_folder = Path(input_folder)

    # --- Validate tools ---
    if not check_tool("ffmpeg"):
        print(
            "Error: ffmpeg not found in PATH. "
            "Please install ffmpeg and ensure it's in PATH.",
            file=sys.stderr,
        )
        return 1
    if not check_tool("ffprobe"):
        print(
            "Error: ffprobe not found in PATH. "
            "Please install ffmpeg (includes ffprobe) and ensure it's in PATH.",
            file=sys.stderr,
        )
        return 1

    # --- Validate input folder ---
    if not input_folder.is_dir():
        print(f"Error: Input folder not found: {input_folder}", file=sys.stderr)
        return 1

    # --- Find audio files ---
    audio_files = find_audio_files(input_folder, file_pattern)
    if not audio_files:
        print(
            f"Error: No audio files found in '{input_folder}'. "
            f"Supported extensions: {', '.join(sorted(AUDIO_EXTENSIONS))}",
            file=sys.stderr,
        )
        return 1

    if not quiet:
        print(f"Found {len(audio_files)} audio file(s) in '{input_folder}'\n")

    # --- Calculate total duration ---
    if not quiet:
        print("Analyzing track durations...")
    track_durations: List[Tuple[Path, float]] = []
    total_seconds = 0.0
    for f in audio_files:
        try:
            dur = get_audio_duration(f)
        except RuntimeError as exc:
            print(f"  Warning: {exc}", file=sys.stderr)
            continue
        total_seconds += dur
        track_durations.append((f, dur))
        if not quiet:
            print(f"  {f.name}: {format_duration_short(dur)}")

    if not track_durations:
        print("Error: Could not read any audio durations.", file=sys.stderr)
        return 1

    # Effective target after LP multiplier
    multiplier = MODE_MULTIPLIERS.get(lp_mode.upper(), 1)
    effective_minutes = target_minutes * multiplier
    target_seconds = effective_minutes * 60.0
    target_dur = format_duration_short(target_seconds)

    if not quiet:
        print()
        print(f"MiniDisc mode:        {lp_mode}")
        print(
            f"Total album duration: {format_duration_short(total_seconds)}"
            f" ({round(total_seconds / 60, 2)} minutes)"
        )
        print(f"Target duration:      {target_dur} ({effective_minutes} minutes, {lp_mode})")

    # --- Check if resizing is needed ---
    if total_seconds <= target_seconds:
        if not quiet:
            print()
            print(
                f"Album already fits within {effective_minutes} minutes ({lp_mode}). "
                "No resizing needed!"
            )
        return 0

    # --- Calculate speed factor ---
    speed_factor = total_seconds / target_seconds
    percent_increase = (speed_factor - 1) * 100
    new_total = total_seconds / speed_factor

    if not quiet:
        print()
        print(f"Speed factor required: {speed_factor:.4f}x ({percent_increase:.2f}% faster)")
        print(f"New total duration:    {format_duration_short(new_total)}")
        print()

    mode_description = {
        "speed":       "Speed up (pitch will increase)",
        "timestretch": "Time-stretch (pitch preserved)",
    }.get(mode.lower(), mode)

    if not quiet:
        print(f"Processing mode: {mode_description}")
        print(f"Output format:   {output_format}")
        print()

    # --- Confirmation prompt ---
    if not yes:
        answer = input("Proceed with resizing? [Y/N] (default: Y): ").strip()
        if answer.lower().startswith("n"):
            print("Aborted.")
            return 0

    # --- Resize tracks ---
    input_paths = [f for f, _dur in track_durations]
    results = resize_tracks(
        input_paths,
        output_folder,
        speed_factor,
        mode=mode,
        output_format=output_format,
        quiet=quiet,
    )

    # --- Summary ---
    if not quiet:
        print()
        print("Processing complete!")
        print(f"Output folder: {Path(output_folder).resolve()}")
        print()

        successful = [(p, d) for p, d in results if d is not None]
        if successful:
            actual_total = sum(d for _p, d in successful)
            print("Summary:")
            print(f"  Original total: {format_duration_short(total_seconds)}")
            print(f"  New total:      {format_duration_short(actual_total)}")
            print(f"  Target was:     {format_duration_short(target_seconds)}")

            tolerance = 0.1
            if actual_total <= target_seconds + tolerance:
                print()
                print(
                    f"Success! Album now fits on a {effective_minutes}-minute MiniDisc ({lp_mode})."
                )
            else:
                over_by = actual_total - target_seconds
                print()
                print(
                    f"Warning: Still over by {format_duration_short(over_by)}. "
                    "May need manual adjustment."
                )

    return 0


# ------------------------------------------------------------------ #
#  CLI                                                                 #
# ------------------------------------------------------------------ #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resize (speed up) album tracks proportionally to fit on a MiniDisc. "
            "Requires ffmpeg and ffprobe in PATH."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --input-folder ./MyAlbum --mode Speed\n"
            "  %(prog)s --input-folder ./MyAlbum --lp2 --mode TimeStretch\n"
            "  %(prog)s --input-folder ./MyAlbum --lp4 --mode Speed --format ALAC\n"
        ),
    )

    parser.add_argument(
        "--input-folder", "-i",
        required=True,
        metavar="FOLDER",
        help="Folder containing the audio tracks to resize.",
    )
    parser.add_argument(
        "--output-folder", "-o",
        default="./Output",
        metavar="FOLDER",
        help="Folder where resized tracks will be saved (default: ./Output).",
    )
    parser.add_argument(
        "--target-minutes", "-t",
        type=float,
        default=74.0,
        metavar="MINUTES",
        help="Base disc capacity in minutes (default: 74). Multiplied by 2 for LP2 or 4 for LP4.",
    )

    lp_group = parser.add_mutually_exclusive_group()
    lp_group.add_argument(
        "--lp2",
        action="store_true",
        help="Enable MDLP LP2 mode (doubles effective disc capacity).",
    )
    lp_group.add_argument(
        "--lp4",
        action="store_true",
        help="Enable MDLP LP4 mode (quadruples effective disc capacity).",
    )

    parser.add_argument(
        "--mode", "-m",
        required=True,
        choices=["Speed", "TimeStretch"],
        metavar="MODE",
        help="Processing mode: Speed (pitch changes) or TimeStretch (pitch preserved).",
    )
    parser.add_argument(
        "--format", "-f",
        dest="output_format",
        default="FLAC",
        choices=list(OUTPUT_FORMATS),
        metavar="FORMAT",
        help=(
            "Output format (all lossless): "
            + ", ".join(OUTPUT_FORMATS)
            + " (default: FLAC)."
        ),
    )
    parser.add_argument(
        "--file-pattern",
        default=None,
        metavar="PATTERN",
        help="Optional glob pattern to filter input files (e.g. '*.mp3').",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    lp_mode = "SP"
    if args.lp2:
        lp_mode = "LP2"
    elif args.lp4:
        lp_mode = "LP4"

    exit_code = resize_album(
        input_folder=args.input_folder,
        output_folder=args.output_folder,
        target_minutes=args.target_minutes,
        lp_mode=lp_mode,
        mode=args.mode,
        output_format=args.output_format,
        file_pattern=args.file_pattern,
        yes=args.yes,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
