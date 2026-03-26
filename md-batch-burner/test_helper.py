#!/usr/bin/env python3
"""
Test script for the MiniDisc Batch Burner
Verifies basic functionality without requiring a MiniDisc device
"""

import sys
from pathlib import Path

# Add project directory to path
project_dir = Path(__file__).parent
sys.path.insert(0, str(project_dir))

from md_batch_burner import Track, Disc, DiscConfig, DiscStatus


def test_track_creation():
    """Test Track data model."""
    print("Testing Track creation...")
    track = Track(
        path="/music/song.mp3",
        name="My Song",
        duration_seconds=245.5
    )

    assert track.path == "/music/song.mp3"
    assert track.name == "My Song"
    assert track.duration_seconds == 245.5
    assert track.duration_display == "4:05"
    print("  ✓ Track creation works")


def test_disc_config():
    """Test DiscConfig data model."""
    print("Testing DiscConfig...")
    config = DiscConfig(disc_size=74, mode="SP")

    assert config.disc_size == 74
    assert config.mode == "SP"
    assert config.capacity_seconds == 74 * 60  # 4440 seconds
    print("  ✓ DiscConfig works")

    # Test LP2 mode
    config_lp2 = DiscConfig(disc_size=74, mode="LP2")
    assert config_lp2.capacity_seconds == 74 * 60 * 2  # 8880 seconds
    print("  ✓ LP2 mode capacity works")


def test_disc_creation():
    """Test Disc data model."""
    print("Testing Disc creation...")
    config = DiscConfig(disc_size=74, mode="SP")
    disc = Disc(id=1, config=config)

    assert disc.id == 1
    assert disc.config == config
    assert len(disc.tracks) == 0
    assert disc.total_seconds == 0
    assert disc.status == DiscStatus.READY
    print("  ✓ Disc creation works")


def test_disc_with_tracks():
    """Test Disc with tracks."""
    print("Testing Disc with tracks...")
    config = DiscConfig(disc_size=74, mode="SP")
    disc = Disc(id=1, config=config)

    track1 = Track(path="/music/1.mp3", name="Track 1", duration_seconds=180)
    track2 = Track(path="/music/2.mp3", name="Track 2", duration_seconds=240)

    disc.tracks.extend([track1, track2])
    disc.total_seconds = 420

    assert len(disc.tracks) == 2
    assert disc.total_seconds == 420
    assert disc.used_display == "7:00"
    assert disc.capacity_display == "74:00"
    assert disc.progress_percent == (420 / 4440) * 100
    print("  ✓ Disc with tracks works")


def test_disc_splitting():
    """Test greedy disc splitting algorithm."""
    print("Testing disc splitting algorithm...")
    tracks = [
        Track(path=f"/music/{i}.mp3", name=f"Track {i}", duration_seconds=300)
        for i in range(20)
    ]

    config = DiscConfig(disc_size=74, mode="SP")  # 4440 seconds
    discs = []
    current_disc = Disc(id=1, config=config)

    for track in tracks:
        if current_disc.total_seconds + track.duration_seconds > current_disc.config.capacity_seconds:
            discs.append(current_disc)
            current_disc = Disc(id=len(discs) + 1, config=config)

        current_disc.tracks.append(track)
        current_disc.total_seconds += track.duration_seconds

    if current_disc.tracks:
        discs.append(current_disc)

    # Should fit ~14 tracks per disc (14 * 300 = 4200 seconds)
    assert len(discs) == 2
    assert len(discs[0].tracks) == 14
    assert len(discs[1].tracks) == 6
    print(f"  ✓ Created {len(discs)} discs")
    print(f"    Disc 1: {len(discs[0].tracks)} tracks ({discs[0].used_display})")
    print(f"    Disc 2: {len(discs[1].tracks)} tracks ({discs[1].used_display})")


def main():
    """Run all tests."""
    print("=" * 50)
    print("MiniDisc Batch Burner - Test Suite")
    print("=" * 50)
    print()

    try:
        test_track_creation()
        test_disc_config()
        test_disc_creation()
        test_disc_with_tracks()
        test_disc_splitting()

        print()
        print("=" * 50)
        print("All tests passed! ✅")
        print("=" * 50)
        return 0

    except AssertionError as e:
        print()
        print("=" * 50)
        print(f"Test failed: {e}")
        print("=" * 50)
        return 1
    except Exception as e:
        print()
        print("=" * 50)
        print(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 50)
        return 1


if __name__ == "__main__":
    sys.exit(main())
