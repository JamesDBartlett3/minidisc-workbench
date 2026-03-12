# Plan: MiniDisc Playlist Splitter GUI

## Summary

Build a **PySide6** GUI application that lets users drag-and-drop music files,
detect their durations via **mutagen**, and intelligently split them across
multiple MiniDiscs with per-disc configurable sizes (60/74/80 min) and recording
modes (SP/LP2/LP4). Each disc can independently mix-and-match size and mode.
Supports both ordered (sequential) and optimized (reorderable) splitting modes.

**Single-file application**: `md_playlist_splitter.py` (matches project convention
of self-contained scripts).

---

## User Decisions

| Decision            | Choice                                                  |
| ------------------- | ------------------------------------------------------- |
| **GUI Framework**   | PySide6 (Qt) — native look, built-in drag-and-drop      |
| **Track Ordering**  | Both modes — user toggle between sequential & optimized |
| **SVG Integration** | Not included — keep focused on splitting                |

---

## MiniDisc Capacity Matrix

| Disc Size | SP (1×) | LP2 (2×) | LP4 (4×) |
| --------- | ------- | -------- | -------- |
| 60 min    | 60 min  | 120 min  | 240 min  |
| 74 min    | 74 min  | 148 min  | 296 min  |
| 80 min    | 80 min  | 160 min  | 320 min  |

Supported audio inputs: mp3, flac, wav, m4a, ogg, opus, wma, aac, aiff
(matches existing project).

---

## Target UI Layout

```
┌─ MiniDisc Playlist Splitter ──────────────────────────────────────┐
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │     🎵 Drop music files here (or click to browse)           │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  Default: [74 min ▼] [SP ▼]   Mode: (•) Ordered  ( ) Optimized   │
│  [▶ Auto-Split]  [↺ Reset]  [📋 Export Listing]                   │
│                                                                    │
│ ┌─ Disc 1 ───── 74 min SP ───── 68:32 / 74:00 (92.6%) ────────┐ │
│ │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░                  │ │
│ │  1. Artist - Track One ........................... 4:32       │ │
│ │  2. Artist - Track Two ........................... 3:58       │ │
│ │  3. Artist - Track Three ......................... 5:11       │ │
│ │  [60|74|80]  [SP|LP2|LP4]                                    │ │
│ └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│ ┌─ Disc 2 ───── 80 min LP2 ──── 142:10 / 160:00 (88.9%) ──────┐ │
│ │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░                  │ │
│ │  4. Artist - Track Four .......................... 6:22       │ │
│ │  ...                                                         │ │
│ └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  Summary: 3 discs │ 47 tracks │ 3:22:15 total                     │
│  Shopping list: 2× 74-min, 1× 80-min                              │
└────────────────────────────────────────────────────────────────────┘
```

---

## Architecture

### Dependencies

| Package | Purpose                                 |
| ------- | --------------------------------------- |
| PySide6 | GUI framework with native drag-and-drop |
| mutagen | Pure-Python audio metadata reader       |

### Data Model (dataclasses)

- `Track`: path, title, artist, album, duration_seconds
- `DiscConfig`: disc_size (60/74/80), mode (SP/LP2/LP4), computed effective_capacity_seconds
- `Disc`: config, list[Track], usage stats (total_seconds, remaining, percent_used)
- `SplitResult`: list[Disc], total tracks, total duration, efficiency %

### Splitting Algorithms

1. **Sequential (ordered)**: Greedy — pack consecutive tracks until disc full,
   start next disc with default config. Preserves playlist order.
2. **Optimized (reorderable)**: First Fit Decreasing — sort tracks by duration
   descending, assign each to the first disc with room. Tries all 9 disc
   configs to minimize disc count.

---

## Implementation Steps

### Phase 1: Core Data Model & Audio Detection

1. Define `Track`, `DiscConfig`, `Disc`, `SplitResult` dataclasses and MiniDisc
   capacity constants.
2. Implement `scan_audio_files(paths) -> list[Track]` using mutagen to extract
   title, artist, album, duration from all 9 supported formats. Fallback to
   filename parsing if metadata missing.
3. Implement `format_duration(seconds) -> str` helper (MM:SS format, matching
   existing project style).

### Phase 2: Splitting Algorithms

4. Implement `split_sequential(tracks, default_config) -> SplitResult` — greedy
   ordered packing. _(depends on step 1)_
5. Implement `split_optimized(tracks, preferred_configs) -> SplitResult` — First
   Fit Decreasing with multi-config optimization. Try all 9 disc configs, use
   heuristic to find minimum disc count. _(depends on step 1)_
6. Implement `recalculate_disc(disc) -> disc` — recompute usage stats when disc
   config changes. _(depends on step 1)_

### Phase 3: Main Window & Drop Zone _(parallel with Phase 2)_

7. Create `PlaylistSplitterApp(QMainWindow)` with dark-themed QSS styling
   (charcoal background, silver/blue accents — MiniDisc aesthetic).
8. Implement drop zone widget (`DropZone(QLabel)`) — accepts file drops via
   `dragEnterEvent`/`dropEvent`, filters for audio extensions, also has
   click-to-browse via `QFileDialog`.
9. Add toolbar controls: default disc size combo, default mode combo,
   ordered/optimized radio toggle, auto-split button, reset button, export
   button.

### Phase 4: Disc Visualization Widgets

10. Create `DiscWidget(QFrame)` — shows one disc: header (disc number, config,
    usage), colored capacity bar, scrollable track list, per-disc size/mode
    selectors.
11. Create `CapacityBar(QWidget)` — custom-painted widget showing track segments
    as colored blocks proportional to their duration within the disc capacity.
    Each track gets a distinct color from a palette.
12. Implement disc panel area (`QScrollArea`) containing vertically stacked
    `DiscWidget` instances.

### Phase 5: Interactive Features

13. Wire auto-split button: reads drop zone files → scans durations → runs
    selected algorithm → populates disc widgets.
14. Implement per-disc config changes: when user changes a disc's size/mode,
    recalculate capacity and show overflow warnings (red highlight if tracks
    exceed capacity).
15. Implement drag-and-drop tracks between discs using Qt's internal
    drag-and-drop (`QDrag`, custom MIME type). Tracks can be dragged from one
    disc's list to another.
16. Implement "Export Track Listing" — copies a formatted text summary to
    clipboard (disc-by-disc track listing with durations).

### Phase 6: Polish

17. Add summary bar at bottom: total disc count, total tracks, total duration,
    overall efficiency %, and "shopping list" (e.g., "2× 74-min, 1× 80-min").
18. Add overflow/underflow visual feedback: disc headers turn red when over
    capacity, green when well-utilized (>80%), yellow when under-utilized
    (<50%).
19. Add keyboard shortcuts: Ctrl+O (browse files), Ctrl+S (export listing),
    Delete (remove selected track), Ctrl+Z (undo last action).
20. Add smart suggestion banner: when auto-split completes, if switching a disc
    to a different config would eliminate a disc, show a dismissible suggestion
    (e.g., "Tip: Switching Disc 2 to LP2 would fit all remaining tracks").

---

## Key Design Decisions

| Decision                  | Rationale                                                |
| ------------------------- | -------------------------------------------------------- |
| Single file               | Matches project convention of self-contained scripts     |
| mutagen for metadata      | Pure Python, no ffprobe dependency for reading durations |
| PySide6 (not tkinter)     | User chose Qt for native look and built-in DnD           |
| Dark theme                | MiniDisc-era aesthetic with charcoal/silver/blue palette |
| Shopping list             | Creative addition — tells user which blank discs to buy  |
| Per-disc config selectors | Core requirement: mix-and-match modes & disc lengths     |

---

## Verification Checklist

- [ ] `python md_playlist_splitter.py` — app opens with drop zone visible
- [ ] Drag audio files from file explorer → tracks appear with correct titles/durations
- [ ] Auto-Split with SP/74-min → tracks split correctly, no disc exceeds 74 min
- [ ] Change a disc from SP to LP2 → capacity bar updates to 148 min
- [ ] Toggle "Optimized" and re-split → disc count ≤ sequential count
- [ ] Drag a track from Disc 1 to Disc 2 → track moves, both bars update
- [ ] Export → clipboard has formatted listing
- [ ] Drop files totaling >320 min → multiple discs, shopping list correct
- [ ] Single track longer than max disc capacity → warning shown
