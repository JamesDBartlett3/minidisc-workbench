"""
pytest tests for resize_album_to_fit_md.py

Mirrors the Pester tests in Resize-AlbumToFitMD.Tests.ps1.
Uses ffmpeg to generate synthetic WAV test fixtures with known durations,
then verifies the resized output with ffprobe.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from resize_album_to_fit_md import (
    build_atempo_chain,
    calculate_speed_factor,
    compute_playlist_resize_factor,
    find_audio_files,
    format_duration_long,
    format_duration_short,
    get_audio_duration,
    resize_album,
    resize_tracks,
)

# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

# Skip the whole test module if ffmpeg / ffprobe are not available
pytestmark = pytest.mark.skipif(
    not (
        subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
        and subprocess.run(["ffprobe", "-version"], capture_output=True).returncode == 0
    ),
    reason="ffmpeg / ffprobe not found in PATH",
)


def make_sine_wav(path: Path, duration_seconds: float) -> None:
    """Create a 440 Hz sine-wave WAV file of *duration_seconds* using ffmpeg."""
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}:sample_rate=44100",
            "-c:a", "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


def make_input_folder(tmp_path: Path, durations: list[float], sub: str = "input") -> Path:
    """Create a temp input folder with sine-wave WAVs of the given durations."""
    folder = tmp_path / sub
    folder.mkdir(parents=True, exist_ok=True)
    for i, dur in enumerate(durations, start=1):
        make_sine_wav(folder / f"Track{i:02d}.wav", dur)
    return folder


# ------------------------------------------------------------------ #
#  Unit tests — pure Python helpers                                    #
# ------------------------------------------------------------------ #


class TestFormatDurationShort:
    def test_whole_minutes(self):
        assert format_duration_short(74 * 60) == "74:00"

    def test_mixed(self):
        assert format_duration_short(3661) == "61:01"

    def test_zero(self):
        assert format_duration_short(0) == "0:00"

    def test_negative_clamped(self):
        assert format_duration_short(-5) == "0:00"


class TestFormatDurationLong:
    def test_with_milliseconds(self):
        assert format_duration_long(74 * 60 + 1.5) == "74:01.500"

    def test_zero(self):
        assert format_duration_long(0) == "0:00.000"


class TestBuildAtempoChain:
    def test_simple(self):
        assert build_atempo_chain(1.5) == "atempo=1.5"

    def test_factor_above_2(self):
        result = build_atempo_chain(3.0)
        assert result == "atempo=2.0,atempo=1.5"

    def test_factor_at_2(self):
        assert build_atempo_chain(2.0) == "atempo=2.0"

    def test_factor_below_half(self):
        result = build_atempo_chain(0.25)
        assert result == "atempo=0.5,atempo=0.5"

    def test_factor_at_half(self):
        assert build_atempo_chain(0.5) == "atempo=0.5"

    def test_factor_above_4(self):
        # 5.0 → 2.0 × 2.0 × 1.25
        result = build_atempo_chain(5.0)
        assert result == "atempo=2.0,atempo=2.0,atempo=1.25"

    def test_factor_1(self):
        assert build_atempo_chain(1.0) == "atempo=1.0"


class TestCalculateSpeedFactor:
    def test_basic(self):
        # 90s total → fit into 1 min = 60s → factor 1.5
        assert abs(calculate_speed_factor(90, 1) - 1.5) < 1e-9

    def test_lp2_doubles_target(self):
        # LP2 doubles target minutes: 0.5 min × 2 = 1 min = 60s
        assert abs(calculate_speed_factor(90, 0.5, "LP2") - 1.5) < 1e-9

    def test_lp4_quadruples_target(self):
        # LP4 quadruples target minutes: 0.25 min × 4 = 1 min = 60s
        assert abs(calculate_speed_factor(90, 0.25, "LP4") - 1.5) < 1e-9

    def test_already_fits_returns_lt1(self):
        # 30s into 1 min → factor < 1 (no resizing needed)
        assert calculate_speed_factor(30, 1) < 1.0

    def test_zero_target_raises(self):
        with pytest.raises(ValueError):
            calculate_speed_factor(90, 0)


class TestFindAudioFiles:
    def test_returns_sorted_audio_files(self, tmp_dir):
        (tmp_dir / "b.mp3").touch()
        (tmp_dir / "a.flac").touch()
        (tmp_dir / "notes.txt").touch()
        files = find_audio_files(tmp_dir)
        names = [f.name for f in files]
        assert names == sorted(names)
        assert "notes.txt" not in names
        assert len(files) == 2

    def test_file_pattern(self, tmp_dir):
        (tmp_dir / "track1.mp3").touch()
        (tmp_dir / "track2.flac").touch()
        files = find_audio_files(tmp_dir, "*.mp3")
        assert len(files) == 1
        assert files[0].suffix == ".mp3"

    def test_empty_folder(self, tmp_dir):
        assert find_audio_files(tmp_dir) == []


# ------------------------------------------------------------------ #
#  Integration tests — require ffmpeg                                  #
# ------------------------------------------------------------------ #


class TestGetAudioDuration:
    def test_known_duration(self, tmp_path):
        wav = tmp_path / "test.wav"
        make_sine_wav(wav, 10.0)
        dur = get_audio_duration(wav)
        assert abs(dur - 10.0) < 0.1

    def test_invalid_file_raises(self, tmp_path):
        txt = tmp_path / "not_audio.txt"
        txt.write_text("hello")
        with pytest.raises(RuntimeError):
            get_audio_duration(txt)


class TestResizeTracks:
    def test_speed_mode_output_count(self, tmp_path):
        folder = make_input_folder(tmp_path, [20.0, 20.0])
        output = tmp_path / "out"
        results = resize_tracks(list(folder.glob("*.wav")), output, 1.5, mode="Speed", quiet=True)
        assert len(results) == 2
        # Both should succeed
        assert all(d is not None for _p, d in results)

    def test_speed_mode_shortens_duration(self, tmp_path):
        folder = make_input_folder(tmp_path, [30.0])
        output = tmp_path / "out"
        results = resize_tracks(list(folder.glob("*.wav")), output, 1.5, mode="Speed", quiet=True)
        _path, dur = results[0]
        assert dur is not None
        # Expected ~20s; allow ±1s tolerance
        assert abs(dur - 20.0) <= 1.0

    def test_output_format_flac(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0])
        output = tmp_path / "out"
        resize_tracks(list(folder.glob("*.wav")), output, 1.5, output_format="FLAC", quiet=True)
        assert len(list(output.glob("*.flac"))) == 1

    def test_output_format_wav(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0])
        output = tmp_path / "out"
        resize_tracks(list(folder.glob("*.wav")), output, 1.5, output_format="WAV", quiet=True)
        assert len(list(output.glob("*.wav"))) == 1

    def test_output_format_aiff(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0])
        output = tmp_path / "out"
        resize_tracks(list(folder.glob("*.wav")), output, 1.5, output_format="AIFF", quiet=True)
        assert len(list(output.glob("*.aiff"))) == 1

    def test_invalid_format_raises(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0])
        with pytest.raises(ValueError, match="Unsupported output format"):
            resize_tracks(list(folder.glob("*.wav")), tmp_path / "out", 1.5, output_format="MP3", quiet=True)

    def test_invalid_mode_raises(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0])
        with pytest.raises(ValueError, match="Unsupported mode"):
            resize_tracks(list(folder.glob("*.wav")), tmp_path / "out", 1.5, mode="Warp", quiet=True)

    def test_proportionality(self, tmp_path):
        """All tracks should be shortened by the same ratio."""
        folder = make_input_folder(tmp_path, [20.0, 30.0, 40.0])
        output = tmp_path / "out"
        factor = 90.0 / 60.0  # 1.5x
        input_files = sorted(folder.glob("*.wav"))
        results = resize_tracks(input_files, output, factor, mode="Speed", quiet=True)
        original_durations = [20.0, 30.0, 40.0]
        # Allow 2% relative tolerance: ffmpeg's atempo filter introduces small
        # floating-point rounding in the tempo value and the output container may
        # pad/trim by a few milliseconds, so exact equality cannot be expected.
        _RATIO_TOL = 0.02
        for (_, actual_dur), expected_orig in zip(results, original_durations):
            assert actual_dur is not None
            ratio = expected_orig / actual_dur
            assert ratio == pytest.approx(factor, rel=_RATIO_TOL)


class TestResizeAlbum:
    def test_already_fits_exits_0(self, tmp_path):
        folder = make_input_folder(tmp_path, [10.0, 10.0, 10.0])
        output = tmp_path / "out"
        code = resize_album(folder, output, target_minutes=5, lp_mode="SP", mode="Speed", yes=True, quiet=True)
        # 30s < 5 min → no resizing needed
        assert code == 0
        # No output files should have been created
        assert not list(output.glob("*")) if output.exists() else True

    def test_invalid_input_folder(self, tmp_path):
        code = resize_album(tmp_path / "nonexistent", tmp_path / "out", yes=True, quiet=True, mode="Speed")
        assert code == 1

    def test_empty_folder_exits_1(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        code = resize_album(empty, tmp_path / "out", yes=True, quiet=True, mode="Speed")
        assert code == 1

    def test_sp_resize_fits_target(self, tmp_path):
        # 3 tracks of 30s = 90s → target 1 min = 60s → factor 1.5
        folder = make_input_folder(tmp_path, [30.0, 30.0, 30.0])
        output = tmp_path / "out"
        code = resize_album(folder, output, target_minutes=1, lp_mode="SP", mode="Speed", yes=True, quiet=True)
        assert code == 0
        out_files = list(output.glob("*.flac"))
        assert len(out_files) == 3
        total = sum(get_audio_duration(f) for f in out_files)
        assert total <= 61.0  # within 1s of target
        assert total >= 59.0

    def test_lp2_resize_fits_target(self, tmp_path):
        # LP2: 0.5 min × 2 = 1 min = 60s effective target
        folder = make_input_folder(tmp_path, [30.0, 30.0, 30.0])
        output = tmp_path / "out"
        code = resize_album(folder, output, target_minutes=0.5, lp_mode="LP2", mode="Speed", yes=True, quiet=True)
        assert code == 0
        total = sum(get_audio_duration(f) for f in output.glob("*.flac"))
        assert total <= 61.0
        assert total >= 59.0

    def test_lp4_resize_fits_target(self, tmp_path):
        # LP4: 0.25 min × 4 = 1 min = 60s effective target
        folder = make_input_folder(tmp_path, [30.0, 30.0, 30.0])
        output = tmp_path / "out"
        code = resize_album(folder, output, target_minutes=0.25, lp_mode="LP4", mode="Speed", yes=True, quiet=True)
        assert code == 0
        total = sum(get_audio_duration(f) for f in output.glob("*.flac"))
        assert total <= 61.0
        assert total >= 59.0

    def test_wav_output_format(self, tmp_path):
        folder = make_input_folder(tmp_path, [20.0, 20.0])
        output = tmp_path / "out"
        resize_album(folder, output, target_minutes=0.5, lp_mode="SP", mode="Speed", output_format="WAV", yes=True, quiet=True)
        assert len(list(output.glob("*.wav"))) == 2

    def test_aiff_output_format(self, tmp_path):
        folder = make_input_folder(tmp_path, [20.0, 20.0])
        output = tmp_path / "out"
        resize_album(folder, output, target_minutes=0.5, lp_mode="SP", mode="Speed", output_format="AIFF", yes=True, quiet=True)
        assert len(list(output.glob("*.aiff"))) == 2


# ------------------------------------------------------------------ #
#  Unit tests for compute_playlist_resize_factor                       #
# ------------------------------------------------------------------ #


class TestComputePlaylistResizeFactor:

    def test_all_tracks_basic(self):
        # 90s total, no fixed tracks, target 60s → factor 1.5
        factor = compute_playlist_resize_factor(90, 0, 60)
        assert factor is not None
        assert abs(factor - 1.5) < 1e-9

    def test_partial_tracks(self):
        # resize 60s, fixed 20s, target 80s → available = 80-20 = 60s, factor = 60/60 = 1.0 (exactly fits)
        factor = compute_playlist_resize_factor(60, 20, 80)
        assert factor is not None
        assert abs(factor - 1.0) < 1e-9

    def test_partial_tracks_over(self):
        # resize 60s, fixed 20s, target 70s → available = 50s, factor = 60/50 = 1.2
        factor = compute_playlist_resize_factor(60, 20, 70)
        assert factor is not None
        assert abs(factor - 1.2) < 1e-9

    def test_fixed_tracks_exceed_target_returns_none(self):
        # fixed=70, target=60 → available ≤ 0 → impossible
        assert compute_playlist_resize_factor(30, 70, 60) is None

    def test_fixed_tracks_equal_target_returns_none(self):
        assert compute_playlist_resize_factor(10, 60, 60) is None

    def test_already_fits_returns_le_1(self):
        # 20s resize + 30s fixed = 50s total, target 60s → factor < 1 (no resize needed)
        factor = compute_playlist_resize_factor(20, 30, 60)
        assert factor is not None
        assert factor <= 1.0

    def test_zero_resize_seconds_returns_none(self):
        assert compute_playlist_resize_factor(0, 30, 60) is None

    def test_zero_target_fixed_all_resize(self):
        # 120s total, no fixed, target 60s → factor = 2.0
        factor = compute_playlist_resize_factor(120, 0, 60)
        assert factor is not None
        assert abs(factor - 2.0) < 1e-9
