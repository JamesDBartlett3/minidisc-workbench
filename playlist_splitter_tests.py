#!/usr/bin/env python3
"""
Unit tests for the core (non-GUI) logic in playlist_splitter.py.

The tests only import pure-logic symbols so they run without tkinter or
any display server.

Run with:
    python3 -m pytest playlist_splitter_tests.py -v
  or directly:
    python3 playlist_splitter_tests.py
"""

import unittest

from playlist_splitter import (
    DISC_CAPACITIES,
    MODE_MULTIPLIERS,
    DiscAssignment,
    Track,
    _parse_drop_data,
    format_duration,
    format_duration_detailed,
    split_playlist,
)

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def make_track(name: str, seconds: float) -> Track:
    return Track(path=f"/fake/{name}", name=name, duration_seconds=seconds)


# ------------------------------------------------------------------ #
#  format_duration                                                     #
# ------------------------------------------------------------------ #


class TestFormatDuration(unittest.TestCase):

    def test_zero(self):
        self.assertEqual(format_duration(0), "0:00")

    def test_negative_clamped_to_zero(self):
        self.assertEqual(format_duration(-5), "0:00")

    def test_under_one_minute(self):
        self.assertEqual(format_duration(45), "0:45")

    def test_exactly_one_minute(self):
        self.assertEqual(format_duration(60), "1:00")

    def test_74_min_disc(self):
        self.assertEqual(format_duration(74 * 60), "74:00")

    def test_lp2_148_min_disc(self):
        self.assertEqual(format_duration(148 * 60), "148:00")

    def test_lp4_296_min_disc(self):
        self.assertEqual(format_duration(296 * 60), "296:00")

    def test_large_value_expressed_as_total_minutes(self):
        # 1 h 1 min 1 s = 3661 s = 61 min 1 s → "61:01"
        self.assertEqual(format_duration(3661), "61:01")

    def test_rounding_up(self):
        # 59.5 rounds to 60 s = 1:00
        self.assertEqual(format_duration(59.5), "1:00")

    def test_rounding_down(self):
        # 59.4 rounds to 59 s = 0:59
        self.assertEqual(format_duration(59.4), "0:59")


class TestFormatDurationDetailed(unittest.TestCase):

    def test_zero(self):
        self.assertEqual(format_duration_detailed(0), "0:00.0")

    def test_with_tenths(self):
        self.assertEqual(format_duration_detailed(90.7), "1:30.7")

    def test_large(self):
        self.assertEqual(format_duration_detailed(74 * 60 + 0.3), "74:00.3")


# ------------------------------------------------------------------ #
#  Track                                                               #
# ------------------------------------------------------------------ #


class TestTrack(unittest.TestCase):

    def test_duration_display_property(self):
        t = make_track("song.mp3", 125.0)
        self.assertEqual(t.duration_display, "2:05")

    def test_track_is_immutable(self):
        t = make_track("a.mp3", 30)
        with self.assertRaises(AttributeError):
            t.name = "b.mp3"  # NamedTuple is read-only


# ------------------------------------------------------------------ #
#  DiscAssignment                                                      #
# ------------------------------------------------------------------ #


class TestDiscAssignment(unittest.TestCase):

    def _disc(self, track_s: float, cap_s: float) -> DiscAssignment:
        return DiscAssignment(
            disc_number=1,
            tracks=[make_track("t.mp3", track_s)],
            total_seconds=track_s,
            capacity_seconds=cap_s,
        )

    def test_remaining_positive_when_under(self):
        d = self._disc(30 * 60, 74 * 60)
        self.assertAlmostEqual(d.remaining_seconds, 44 * 60)

    def test_remaining_negative_when_over(self):
        d = self._disc(80 * 60, 74 * 60)
        self.assertLess(d.remaining_seconds, 0)

    def test_is_over_capacity_false_at_exact_fit(self):
        d = self._disc(74 * 60, 74 * 60)
        self.assertFalse(d.is_over_capacity)

    def test_is_over_capacity_true(self):
        d = self._disc(74 * 60 + 1, 74 * 60)
        self.assertTrue(d.is_over_capacity)


# ------------------------------------------------------------------ #
#  split_playlist — empty input                                        #
# ------------------------------------------------------------------ #


class TestSplitEmpty(unittest.TestCase):

    def test_empty_tracks_returns_empty_list(self):
        self.assertEqual(split_playlist([], 74, "SP"), [])

    def test_empty_for_all_modes(self):
        for mode in MODE_MULTIPLIERS:
            with self.subTest(mode=mode):
                self.assertEqual(split_playlist([], 74, mode), [])

    def test_empty_for_all_capacities(self):
        for cap in DISC_CAPACITIES:
            with self.subTest(cap=cap):
                self.assertEqual(split_playlist([], cap, "SP"), [])


# ------------------------------------------------------------------ #
#  split_playlist — single disc                                        #
# ------------------------------------------------------------------ #


class TestSplitSingleDisc(unittest.TestCase):

    def test_tracks_that_fit_produce_one_disc(self):
        # 5 × 5 min = 25 min → fits on 74-min disc
        tracks = [make_track(f"t{i}.mp3", 5 * 60) for i in range(5)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].tracks), 5)

    def test_total_exactly_equal_to_capacity_is_one_disc(self):
        tracks = [make_track("t.mp3", 74 * 60)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].is_over_capacity)

    def test_single_track_over_capacity_stays_on_one_disc(self):
        # A single oversized track cannot be split — it gets its own disc.
        tracks = [make_track("big.mp3", 80 * 60)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].is_over_capacity)


# ------------------------------------------------------------------ #
#  split_playlist — overflow triggers new disc                         #
# ------------------------------------------------------------------ #


class TestSplitOverflow(unittest.TestCase):

    def test_one_second_over_capacity_needs_two_discs(self):
        tracks = [make_track("a.mp3", 74 * 60), make_track("b.mp3", 1)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(len(result), 2)

    def test_first_disc_does_not_contain_overflow_track(self):
        tracks = [make_track("a.mp3", 74 * 60), make_track("b.mp3", 1)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(result[0].tracks[0].name, "a.mp3")
        self.assertEqual(result[1].tracks[0].name, "b.mp3")


# ------------------------------------------------------------------ #
#  split_playlist — multiple discs                                     #
# ------------------------------------------------------------------ #


class TestSplitMultipleDiscs(unittest.TestCase):

    def test_even_split_across_two_discs(self):
        # 10 × 8 min = 80 min → needs 2 discs of 74 min
        tracks = [make_track(f"t{i}.mp3", 8 * 60) for i in range(10)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(len(result), 2)

    def test_disc_numbers_are_sequential_from_one(self):
        tracks = [make_track(f"t{i}.mp3", 30 * 60) for i in range(6)]
        result = split_playlist(tracks, 74, "SP")
        for i, disc in enumerate(result, start=1):
            self.assertEqual(disc.disc_number, i)

    def test_track_order_is_preserved_across_discs(self):
        tracks = [make_track(f"track_{i:02d}.mp3", 10 * 60) for i in range(9)]
        result = split_playlist(tracks, 74, "SP")
        flat = [t.name for disc in result for t in disc.tracks]
        self.assertEqual(flat, [t.name for t in tracks])

    def test_every_track_assigned_exactly_once(self):
        tracks = [make_track(f"t{i}.mp3", 12 * 60) for i in range(10)]
        result = split_playlist(tracks, 74, "SP")
        assigned = [t for disc in result for t in disc.tracks]
        self.assertEqual(len(assigned), len(tracks))
        self.assertEqual(
            sorted(t.name for t in assigned),
            sorted(t.name for t in tracks),
        )

    def test_no_disc_exceeds_capacity(self):
        tracks = [make_track(f"t{i}.mp3", 8 * 60) for i in range(20)]
        result = split_playlist(tracks, 74, "SP")
        for disc in result:
            self.assertFalse(disc.is_over_capacity)


# ------------------------------------------------------------------ #
#  split_playlist — LP modes                                           #
# ------------------------------------------------------------------ #


class TestSplitModes(unittest.TestCase):

    def test_sp_capacity_is_1x(self):
        tracks = [make_track("x.mp3", 1)]
        result = split_playlist(tracks, 74, "SP")
        self.assertEqual(result[0].capacity_seconds, 74 * 60 * 1)

    def test_lp2_capacity_is_2x(self):
        tracks = [make_track("x.mp3", 1)]
        result = split_playlist(tracks, 74, "LP2")
        self.assertEqual(result[0].capacity_seconds, 74 * 60 * 2)

    def test_lp4_capacity_is_4x(self):
        tracks = [make_track("x.mp3", 1)]
        result = split_playlist(tracks, 74, "LP4")
        self.assertEqual(result[0].capacity_seconds, 74 * 60 * 4)

    def test_148_min_fits_one_lp2_disc(self):
        tracks = [make_track("a.mp3", 148 * 60)]
        result = split_playlist(tracks, 74, "LP2")
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].is_over_capacity)

    def test_296_min_fits_one_lp4_disc(self):
        tracks = [make_track("a.mp3", 296 * 60)]
        result = split_playlist(tracks, 74, "LP4")
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].is_over_capacity)

    def test_lp4_needs_fewer_discs_than_sp(self):
        # 5 × 40 min = 200 min → 3 SP discs (74 min each) vs 1 LP4 disc (296 min)
        tracks = [make_track(f"t{i}.mp3", 40 * 60) for i in range(5)]
        sp_count  = len(split_playlist(tracks, 74, "SP"))
        lp4_count = len(split_playlist(tracks, 74, "LP4"))
        self.assertGreater(sp_count, lp4_count)

    def test_lp2_needs_fewer_discs_than_sp(self):
        tracks = [make_track(f"t{i}.mp3", 40 * 60) for i in range(5)]
        sp_count  = len(split_playlist(tracks, 74, "SP"))
        lp2_count = len(split_playlist(tracks, 74, "LP2"))
        self.assertGreater(sp_count, lp2_count)


# ------------------------------------------------------------------ #
#  split_playlist — disc capacities                                    #
# ------------------------------------------------------------------ #


class TestDiscCapacities(unittest.TestCase):

    def test_60_min_disc_capacity(self):
        tracks = [make_track("x.mp3", 1)]
        result = split_playlist(tracks, 60, "SP")
        self.assertEqual(result[0].capacity_seconds, 60 * 60)

    def test_74_min_disc_fits_exactly(self):
        tracks = [make_track("a.mp3", 74 * 60)]
        result = split_playlist(tracks, 74, "SP")
        self.assertFalse(result[0].is_over_capacity)

    def test_80_min_disc_fits_exactly(self):
        tracks = [make_track("a.mp3", 80 * 60)]
        result = split_playlist(tracks, 80, "SP")
        self.assertFalse(result[0].is_over_capacity)

    def test_80_min_disc_larger_than_74(self):
        # 4 × 20 min = 80 min → fits one 80-min disc but needs 2 × 74-min discs
        tracks = [make_track(f"t{i}.mp3", 20 * 60) for i in range(4)]
        result_74 = split_playlist(tracks, 74, "SP")
        result_80 = split_playlist(tracks, 80, "SP")
        self.assertGreater(len(result_74), len(result_80))
        self.assertEqual(len(result_80), 1)


# ------------------------------------------------------------------ #
#  _parse_drop_data                                                    #
# ------------------------------------------------------------------ #


class TestParseDropData(unittest.TestCase):

    def test_two_simple_paths(self):
        self.assertEqual(
            _parse_drop_data("/a/b/c.mp3 /d/e/f.mp3"),
            ["/a/b/c.mp3", "/d/e/f.mp3"],
        )

    def test_path_with_spaces_in_braces(self):
        self.assertEqual(
            _parse_drop_data("{/path with spaces/track.mp3} /simple.mp3"),
            ["/path with spaces/track.mp3", "/simple.mp3"],
        )

    def test_single_path(self):
        self.assertEqual(_parse_drop_data("/music/song.flac"), ["/music/song.flac"])

    def test_empty_string(self):
        self.assertEqual(_parse_drop_data(""), [])


# ------------------------------------------------------------------ #
#  Constants sanity                                                    #
# ------------------------------------------------------------------ #


class TestConstants(unittest.TestCase):

    def test_disc_capacities_contains_standard_values(self):
        for v in (60, 74, 80):
            self.assertIn(v, DISC_CAPACITIES)

    def test_mode_multipliers_correct(self):
        self.assertEqual(MODE_MULTIPLIERS["SP"],  1)
        self.assertEqual(MODE_MULTIPLIERS["LP2"], 2)
        self.assertEqual(MODE_MULTIPLIERS["LP4"], 4)


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    unittest.main(verbosity=2)
