#!/usr/bin/env python3
"""
MiniDisc Playlist Splitter
--------------------------
A GUI tool to split a long playlist across multiple MiniDiscs.

Supports:
  - Disc capacities: 60, 74, and 80 minutes
  - Play modes:      SP (1×), LP2 (2×), LP4 (4×)
  - Drag-and-drop file addition (via tkinterdnd2 when available)
  - Manual reordering of tracks within the playlist
  - Greedy packing: fills each disc in order before starting the next

Requirements:
  - Python 3.8+
  - mutagen  (pip install mutagen)      — reads audio metadata/duration
  - tkinterdnd2 (pip install tkinterdnd2) — optional, enables OS file-drop
  - ffprobe in PATH                     — optional fallback for duration

Run:
  python3 playlist_splitter.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

# ------------------------------------------------------------------ #
#  Optional dependencies                                               #
# ------------------------------------------------------------------ #

try:
    from mutagen import File as MutagenFile  # type: ignore
    _MUTAGEN_AVAILABLE = True
except ImportError:
    _MUTAGEN_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    _TKINTER_AVAILABLE = True
except ImportError:
    _TKINTER_AVAILABLE = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #

DISC_CAPACITIES: List[int] = [60, 74, 80]          # minutes
MODE_MULTIPLIERS: "dict[str, int]" = {"SP": 1, "LP2": 2, "LP4": 4}

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus",
    ".wma", ".aac", ".aiff", ".ape", ".wv", ".tta",
}

# ------------------------------------------------------------------ #
#  Core data types                                                     #
# ------------------------------------------------------------------ #


class Track(NamedTuple):
    """An audio track with its file path and duration in seconds."""

    path: str
    name: str
    duration_seconds: float

    @property
    def duration_display(self) -> str:
        """Human-readable m:ss string."""
        return format_duration(self.duration_seconds)


class DiscAssignment(NamedTuple):
    """One disc's worth of tracks and timing metadata."""

    disc_number: int
    tracks: List[Track]
    total_seconds: float
    capacity_seconds: float

    @property
    def remaining_seconds(self) -> float:
        return self.capacity_seconds - self.total_seconds

    @property
    def is_over_capacity(self) -> bool:
        return self.total_seconds > self.capacity_seconds


# ------------------------------------------------------------------ #
#  Duration helpers                                                    #
# ------------------------------------------------------------------ #


def format_duration(seconds: float) -> str:
    """
    Format *seconds* as ``m:ss`` (total minutes, matching MiniDisc convention).

    Times are always expressed as total minutes rather than hours + minutes so
    that values such as 74:00, 148:00, or 296:00 are immediately recognisable
    as disc capacities.

    >>> format_duration(74 * 60)
    '74:00'
    >>> format_duration(3661)
    '61:01'
    >>> format_duration(0)
    '0:00'
    >>> format_duration(-5)
    '0:00'
    """
    if seconds < 0:
        seconds = 0.0
    total_s = int(round(seconds))
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}"


def format_duration_detailed(seconds: float) -> str:
    """Format *seconds* as ``m:ss.t`` (tenths of a second, total minutes)."""
    if seconds < 0:
        seconds = 0.0
    total_s = int(seconds)
    tenths = int((seconds - total_s) * 10)
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}.{tenths}"


# ------------------------------------------------------------------ #
#  Audio duration reading                                              #
# ------------------------------------------------------------------ #


def get_duration_mutagen(path: str) -> Optional[float]:
    """Return audio duration (seconds) using mutagen, or ``None`` on failure."""
    if not _MUTAGEN_AVAILABLE:
        return None
    try:
        audio = MutagenFile(path)
        if audio is not None:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def get_duration_ffprobe(path: str) -> Optional[float]:
    """Return audio duration (seconds) using ffprobe, or ``None`` on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw:
                return float(raw)
    except Exception:
        pass
    return None


def get_duration(path: str) -> Optional[float]:
    """Return audio duration in seconds, trying mutagen then ffprobe."""
    duration = get_duration_mutagen(path)
    if duration is None:
        duration = get_duration_ffprobe(path)
    return duration


# ------------------------------------------------------------------ #
#  Core algorithm                                                      #
# ------------------------------------------------------------------ #


def split_playlist(
    tracks: List[Track],
    disc_capacity_minutes: int,
    mode: str,
) -> List[DiscAssignment]:
    """
    Greedily assign *tracks* to discs in order.

    Each disc has an effective capacity of::

        disc_capacity_minutes × MODE_MULTIPLIERS[mode]  seconds

    Tracks are added to the current disc until adding the *next* track would
    exceed the capacity, at which point a new disc is started.  A single track
    that is longer than one disc's capacity is placed alone on its own disc
    (marked ``is_over_capacity``).

    Parameters
    ----------
    tracks:
        Ordered list of :class:`Track` objects.
    disc_capacity_minutes:
        Base disc capacity in minutes (60, 74, or 80).
    mode:
        Play mode: ``"SP"``, ``"LP2"``, or ``"LP4"``.

    Returns
    -------
    List of :class:`DiscAssignment` objects (one per disc, never empty
    unless *tracks* is empty).
    """
    if not tracks:
        return []

    multiplier = MODE_MULTIPLIERS.get(mode, 1)
    capacity_seconds = disc_capacity_minutes * 60 * multiplier

    discs: List[DiscAssignment] = []
    current_tracks: List[Track] = []
    current_total = 0.0

    for track in tracks:
        # Start a new disc if adding this track would overflow (and the
        # current disc already has at least one track).
        if current_tracks and current_total + track.duration_seconds > capacity_seconds:
            discs.append(
                DiscAssignment(
                    disc_number=len(discs) + 1,
                    tracks=list(current_tracks),
                    total_seconds=current_total,
                    capacity_seconds=capacity_seconds,
                )
            )
            current_tracks = []
            current_total = 0.0

        current_tracks.append(track)
        current_total += track.duration_seconds

    # Flush the final (possibly partial) disc.
    if current_tracks:
        discs.append(
            DiscAssignment(
                disc_number=len(discs) + 1,
                tracks=list(current_tracks),
                total_seconds=current_total,
                capacity_seconds=capacity_seconds,
            )
        )

    return discs


# ------------------------------------------------------------------ #
#  GUI                                                                 #
# ------------------------------------------------------------------ #


class PlaylistSplitterApp:
    """
    Main application window.

    Layout
    ------
    ┌──────────────────────────────────────────────────────────────────┐
    │  [Add Files]  [Remove]  [↑]  [↓]  [Clear]  │ capacity │ mode   │ toolbar
    ├──────────────────────────────────────────────────────────────────┤
    │  # │ Name │ Duration                                            │ track list
    │  Drop audio files here or use Add Files…                        │
    ├──────────────────────────────────────────────────────────────────┤
    │  [Split Playlist]              Total: mm:ss                      │ action bar
    ├──────────────────────────────────────────────────────────────────┤
    │  Disc 1 │ Disc 2 │ … │ Summary  (tabs)                         │ results
    └──────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title("MiniDisc Playlist Splitter")
        self.root.minsize(720, 540)

        self._tracks: List[Track] = []
        self._capacity_var = tk.IntVar(value=74)
        self._mode_var = tk.StringVar(value="SP")

        self._build_ui()
        self._register_dnd()

    # ---------------------------------------------------------------- #
    #  UI construction                                                   #
    # ---------------------------------------------------------------- #

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(3, weight=1)

        self._build_toolbar(row=0)
        self._build_track_list(row=1)
        self._build_action_bar(row=2)
        self._build_results(row=3)

    def _build_toolbar(self, row: int) -> None:
        bar = ttk.Frame(self.root, padding=4)
        bar.grid(row=row, column=0, sticky="ew")

        ttk.Button(bar, text="Add Files", command=self._add_files).pack(side="left", padx=2)
        ttk.Button(bar, text="Remove",    command=self._remove_selected).pack(side="left", padx=2)
        ttk.Button(bar, text="↑", width=3, command=self._move_up).pack(side="left", padx=2)
        ttk.Button(bar, text="↓", width=3, command=self._move_down).pack(side="left", padx=2)
        ttk.Button(bar, text="Clear",     command=self._clear_tracks).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(bar, text="Capacity:").pack(side="left", padx=(0, 2))
        for cap in DISC_CAPACITIES:
            ttk.Radiobutton(
                bar, text=f"{cap} min",
                variable=self._capacity_var, value=cap,
            ).pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(bar, text="Mode:").pack(side="left", padx=(0, 2))
        for mode in MODE_MULTIPLIERS:
            ttk.Radiobutton(
                bar, text=mode,
                variable=self._mode_var, value=mode,
            ).pack(side="left", padx=2)

    def _build_track_list(self, row: int) -> None:
        frame = ttk.LabelFrame(self.root, text="Playlist", padding=4)
        frame.grid(row=row, column=0, sticky="nsew", padx=6, pady=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        cols = ("#", "Name", "Duration")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        self._tree.heading("#",        text="#")
        self._tree.heading("Name",     text="Name")
        self._tree.heading("Duration", text="Duration")
        self._tree.column("#",        width=40,  anchor="center", stretch=False)
        self._tree.column("Name",     width=420)
        self._tree.column("Duration", width=90,  anchor="center", stretch=False)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._status_var = tk.StringVar(
            value="Add files using the button above, or drop audio files here."
        )
        ttk.Label(frame, textvariable=self._status_var, foreground="gray").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

    def _build_action_bar(self, row: int) -> None:
        bar = ttk.Frame(self.root, padding=4)
        bar.grid(row=row, column=0, sticky="ew", padx=6)

        ttk.Button(bar, text="Split Playlist", command=self._split, width=18).pack(
            side="left", padx=2
        )
        self._total_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._total_var, foreground="steelblue").pack(
            side="left", padx=10
        )

    def _build_results(self, row: int) -> None:
        self._results_frame = ttk.LabelFrame(self.root, text="Results", padding=4)
        self._results_frame.grid(row=row, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._results_frame.columnconfigure(0, weight=1)
        self._results_frame.rowconfigure(0, weight=1)

        self._results_nb = ttk.Notebook(self._results_frame)
        self._results_nb.grid(row=0, column=0, sticky="nsew")

        self._results_hint = ttk.Label(
            self._results_frame,
            text="Press 'Split Playlist' to see disc assignments.",
            foreground="gray",
        )
        self._results_hint.grid(row=1, column=0, pady=4)

    # ---------------------------------------------------------------- #
    #  Drag-and-drop registration                                       #
    # ---------------------------------------------------------------- #

    def _register_dnd(self) -> None:
        """Register OS-level file drop on the track list (requires tkinterdnd2)."""
        if not _DND_AVAILABLE:
            return
        try:
            self._tree.drop_target_register(DND_FILES)       # type: ignore[attr-defined]
            self._tree.dnd_bind("<<Drop>>", self._on_drop)   # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_drop(self, event: "tk.Event[tk.Misc]") -> None:
        """Handle files dropped onto the track list."""
        raw: str = event.data  # type: ignore[attr-defined]
        self._load_paths(_parse_drop_data(raw))

    # ---------------------------------------------------------------- #
    #  Track management                                                  #
    # ---------------------------------------------------------------- #

    def _add_files(self) -> None:
        """Open a native file dialog and load selected audio files."""
        filetypes = [
            ("Audio files", " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))),
            ("All files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(title="Add audio files", filetypes=filetypes)
        self._load_paths(list(paths))

    def _load_paths(self, paths: List[str]) -> None:
        """Validate *paths* and append recognised audio files to the playlist."""
        errors: List[str] = []
        for path in paths:
            p = Path(path)
            if not p.is_file():
                continue
            if p.suffix.lower() not in AUDIO_EXTENSIONS:
                errors.append(f"Skipped (unsupported format): {p.name}")
                continue
            duration = get_duration(str(p))
            if duration is None:
                errors.append(f"Could not read duration: {p.name}")
                continue
            self._tracks.append(Track(path=str(p), name=p.name, duration_seconds=duration))

        self._refresh_track_list()

        if errors:
            messagebox.showwarning(
                "Some files skipped",
                "\n".join(errors[:10]) + ("\n…and more." if len(errors) > 10 else ""),
            )

    def _remove_selected(self) -> None:
        """Remove currently-selected tracks from the playlist."""
        selected = self._tree.selection()
        if not selected:
            return
        indices = sorted((self._tree.index(item) for item in selected), reverse=True)
        for idx in indices:
            del self._tracks[idx]
        self._refresh_track_list()

    def _move_up(self) -> None:
        """Move the selected track(s) one position up in the playlist."""
        selected = self._tree.selection()
        if not selected:
            return
        indices = sorted(self._tree.index(item) for item in selected)
        if indices[0] == 0:
            return
        for idx in indices:
            self._tracks[idx - 1], self._tracks[idx] = self._tracks[idx], self._tracks[idx - 1]
        self._refresh_track_list()
        items = self._tree.get_children()
        for idx in indices:
            self._tree.selection_add(items[idx - 1])

    def _move_down(self) -> None:
        """Move the selected track(s) one position down in the playlist."""
        selected = self._tree.selection()
        if not selected:
            return
        indices = sorted((self._tree.index(item) for item in selected), reverse=True)
        if indices[0] == len(self._tracks) - 1:
            return
        for idx in indices:
            self._tracks[idx], self._tracks[idx + 1] = self._tracks[idx + 1], self._tracks[idx]
        self._refresh_track_list()
        items = self._tree.get_children()
        for idx in indices:
            self._tree.selection_add(items[idx + 1])

    def _clear_tracks(self) -> None:
        """Remove all tracks and reset the results pane."""
        self._tracks.clear()
        self._refresh_track_list()
        self._clear_results()

    def _refresh_track_list(self) -> None:
        """Re-render the track list to match ``self._tracks``."""
        self._tree.delete(*self._tree.get_children())
        total_s = 0.0
        for i, track in enumerate(self._tracks, start=1):
            self._tree.insert("", "end", values=(i, track.name, track.duration_display))
            total_s += track.duration_seconds

        if self._tracks:
            self._status_var.set(
                f"{len(self._tracks)} track(s)  —  Total: {format_duration(total_s)}"
            )
            self._total_var.set(f"Total: {format_duration(total_s)}")
        else:
            self._status_var.set(
                "Add files using the button above, or drop audio files here."
            )
            self._total_var.set("")

    # ---------------------------------------------------------------- #
    #  Splitting & results                                              #
    # ---------------------------------------------------------------- #

    def _split(self) -> None:
        if not self._tracks:
            messagebox.showinfo("No tracks", "Please add audio files first.")
            return
        assignments = split_playlist(
            self._tracks,
            self._capacity_var.get(),
            self._mode_var.get(),
        )
        self._show_results(assignments)

    def _clear_results(self) -> None:
        for tab in self._results_nb.tabs():
            self._results_nb.forget(tab)
        self._results_hint.grid()

    def _show_results(self, assignments: List[DiscAssignment]) -> None:
        """Populate the results notebook: one tab per disc + a summary tab."""
        self._clear_results()
        self._results_hint.grid_remove()

        for disc in assignments:
            tab = ttk.Frame(self._results_nb)
            self._results_nb.add(
                tab,
                text=f" Disc {disc.disc_number} ({format_duration(disc.total_seconds)}) ",
            )
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(0, weight=1)

            cols = ("#", "Name", "Duration")
            tree = ttk.Treeview(tab, columns=cols, show="headings", height=8)
            tree.heading("#",        text="#")
            tree.heading("Name",     text="Name")
            tree.heading("Duration", text="Duration")
            tree.column("#",        width=40,  anchor="center", stretch=False)
            tree.column("Name",     width=400)
            tree.column("Duration", width=90,  anchor="center", stretch=False)

            vsb = ttk.Scrollbar(tab, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
            vsb.grid(row=0, column=1, sticky="ns", pady=4)

            for i, track in enumerate(disc.tracks, start=1):
                tree.insert("", "end", values=(i, track.name, track.duration_display))

            used_pct = (
                disc.total_seconds / disc.capacity_seconds * 100
                if disc.capacity_seconds else 0.0
            )
            remaining = disc.remaining_seconds
            color = "red" if disc.is_over_capacity else "green"
            summary = (
                f"Used: {format_duration(disc.total_seconds)} / "
                f"{format_duration(disc.capacity_seconds)} ({used_pct:.1f}%)  —  "
                + (
                    f"Over by {format_duration(abs(remaining))}"
                    if disc.is_over_capacity
                    else f"Remaining: {format_duration(remaining)}"
                )
            )
            ttk.Label(tab, text=summary, foreground=color).grid(
                row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4)
            )

        # ---- Summary tab ----
        capacity = self._capacity_var.get()
        mode = self._mode_var.get()
        multiplier = MODE_MULTIPLIERS[mode]
        effective_min = capacity * multiplier
        total_tracks = sum(len(d.tracks) for d in assignments)
        total_secs = sum(d.total_seconds for d in assignments)

        summary_tab = ttk.Frame(self._results_nb)
        self._results_nb.add(summary_tab, text=" Summary ")
        summary_tab.columnconfigure(0, weight=1)
        summary_tab.rowconfigure(0, weight=1)

        lines = [
            f"Disc capacity:       {capacity} min (base) × {multiplier} ({mode}) = {effective_min} min",
            f"Total tracks:        {total_tracks}",
            f"Total playlist time: {format_duration(total_secs)}",
            f"Discs required:      {len(assignments)}",
            "",
        ]
        for disc in assignments:
            used_pct = (
                disc.total_seconds / disc.capacity_seconds * 100
                if disc.capacity_seconds else 0.0
            )
            status = "⚠ OVER" if disc.is_over_capacity else "✓"
            lines.append(
                f"  Disc {disc.disc_number}: {len(disc.tracks)} track(s),  "
                f"{format_duration(disc.total_seconds)} / "
                f"{format_duration(disc.capacity_seconds)} "
                f"({used_pct:.1f}%)  {status}"
            )

        txt = tk.Text(summary_tab, wrap="word", relief="flat", height=12)
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        txt.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)


# ------------------------------------------------------------------ #
#  Module-level helper (used by GUI but also testable stand-alone)    #
# ------------------------------------------------------------------ #


def _parse_drop_data(data: str) -> List[str]:
    """
    Parse the Tcl-list string returned by tkinterdnd2 drop events.

    Paths are space-separated; paths containing spaces are wrapped in
    ``{braces}``.

    >>> _parse_drop_data("/a/b/c.mp3 /d/e/f.mp3")
    ['/a/b/c.mp3', '/d/e/f.mp3']
    >>> _parse_drop_data("{/path with spaces/track.mp3} /simple.mp3")
    ['/path with spaces/track.mp3', '/simple.mp3']
    """
    tokens: List[str] = re.findall(r"\{[^}]*\}|\S+", data)
    return [t.strip("{}") for t in tokens]


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #


def main() -> None:
    if not _TKINTER_AVAILABLE:
        print(
            "Error: tkinter is not available.\n"
            "  Debian/Ubuntu:  sudo apt install python3-tk\n"
            "  Other systems:  install the tkinter package for your Python.",
            file=sys.stderr,
        )
        sys.exit(1)

    root = TkinterDnD.Tk() if _DND_AVAILABLE else tk.Tk()
    PlaylistSplitterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
