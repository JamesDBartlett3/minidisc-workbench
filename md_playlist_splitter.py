#!/usr/bin/env python3.13
"""
MiniDisc Playlist Splitter (PySide6 GUI)
----------------------------------------
Drag-and-drop music files into this app to intelligently split a long
playlist across multiple MiniDiscs. Each disc can independently choose
its size (60 / 74 / 80 min) and recording mode (SP / LP2 / LP4).

Supports two splitting strategies:
    • **Ordered**   – greedy sequential packing that preserves playlist order.
    • **Optimized** – First Fit Decreasing that reorders tracks to minimise
                      the number of discs needed.

Requirements:
    PySide6   (pip install PySide6)
    mutagen   (pip install mutagen)

Run:
    python md_playlist_splitter.py
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from mutagen import File as MutagenFile  # type: ignore[import-untyped]

from PySide6.QtCore import (
    QMimeData,
    QPoint,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QFont,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# ------------------------------------------------------------------ #
#  Constants                                                           #
# ------------------------------------------------------------------ #

DISC_SIZES: List[int] = [60, 74, 80]  # minutes
MODE_MULTIPLIERS: dict[str, int] = {"SP": 1, "LP2": 2, "LP4": 4}

# MiniDisc cluster geometry.  A cluster is the minimum allocation unit
# on a MiniDisc: 32 data sectors × 2332 bytes = 74 624 bytes, holding
# 176 sound groups × 11.6 ms = 2.0416 s of SP stereo audio.  In LP2
# and LP4 the same physical cluster holds proportionally more audio
# time (×2 / ×4).  Every track must start on a cluster boundary, so
# the unused tail of its last cluster is wasted.
CLUSTER_SP_SECONDS: float = 176 * 0.0116  # ≈ 2.0416 s

# Additional per-track overhead (in SP seconds) for UTOC entries and
# inter-track boundary structures on the physical disc.  This is on
# top of the cluster-alignment waste computed from each track's actual
# duration.  Scales with recording mode (×2 LP2, ×4 LP4) because the
# same physical overhead consumes more recording-time seconds in
# compressed modes.
TRACK_METADATA_SP_SECONDS: float = 0.3

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".m4a", ".ogg",
    ".opus", ".wma", ".aac", ".aiff",
}

# Track-segment palette (repeating) for the capacity bar.
TRACK_COLOURS = [
    "#5B9BD5", "#E06C75", "#98C379", "#E5C07B",
    "#C678DD", "#56B6C2", "#D19A66", "#61AFEF",
    "#BE5046", "#7EC8E3", "#F4A261", "#A78BFA",
    "#34D399", "#F87171", "#FBBF24", "#818CF8",
]

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def format_duration(seconds: float) -> str:
    """Format *seconds* as ``M:SS`` (total minutes, MiniDisc convention).

    >>> format_duration(74 * 60)
    '74:00'
    >>> format_duration(3661)
    '61:01'
    """
    if seconds < 0:
        seconds = 0.0
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


# ------------------------------------------------------------------ #
#  Data model                                                          #
# ------------------------------------------------------------------ #


@dataclass
class Track:
    """An audio track with metadata and duration."""

    path: str
    title: str
    artist: str
    album: str
    duration_seconds: float

    @property
    def display_name(self) -> str:
        if self.artist:
            return f"{self.artist} \u2013 {self.title}"
        return self.title


@dataclass
class DiscConfig:
    """Configuration for a single MiniDisc."""

    disc_size: int = 74        # 60 / 74 / 80
    mode: str = "SP"           # SP / LP2 / LP4

    @property
    def capacity_seconds(self) -> float:
        return self.disc_size * 60 * MODE_MULTIPLIERS.get(self.mode, 1)

    def label(self) -> str:
        return f"{self.disc_size} min {self.mode}"


@dataclass
class Disc:
    """A single MiniDisc with its configuration and assigned tracks."""

    config: DiscConfig
    tracks: List[Track] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        """Total audio duration of all tracks."""
        return sum(t.duration_seconds for t in self.tracks)

    @property
    def cluster_seconds(self) -> float:
        """Duration of one cluster in the current recording mode."""
        return CLUSTER_SP_SECONDS * MODE_MULTIPLIERS.get(self.config.mode, 1)

    @property
    def track_overhead_seconds(self) -> float:
        """Per-track metadata overhead in the current recording mode."""
        return TRACK_METADATA_SP_SECONDS * MODE_MULTIPLIERS.get(self.config.mode, 1)

    def cluster_waste_for(self, track: Track) -> float:
        """Wasted seconds from cluster alignment for a single track."""
        cs = self.cluster_seconds
        remainder = track.duration_seconds % cs
        return (cs - remainder) if remainder > 0 else 0.0

    @property
    def total_waste_seconds(self) -> float:
        """Total wasted time from cluster alignment across all tracks."""
        return sum(self.cluster_waste_for(t) for t in self.tracks)

    @property
    def total_overhead_seconds(self) -> float:
        """Total per-track metadata overhead across all tracks."""
        return len(self.tracks) * self.track_overhead_seconds

    @property
    def effective_seconds(self) -> float:
        """Total disc usage: audio + cluster waste + metadata overhead."""
        return self.total_seconds + self.total_waste_seconds + self.total_overhead_seconds

    @property
    def remaining_seconds(self) -> float:
        return self.config.capacity_seconds - self.effective_seconds

    @property
    def percent_used(self) -> float:
        cap = self.config.capacity_seconds
        return (self.effective_seconds / cap * 100) if cap > 0 else 0.0

    @property
    def is_over(self) -> bool:
        return self.effective_seconds > self.config.capacity_seconds


@dataclass
class SplitResult:
    """Result of splitting a playlist across discs."""

    discs: List[Disc]

    @property
    def total_tracks(self) -> int:
        return sum(len(d.tracks) for d in self.discs)

    @property
    def total_duration(self) -> float:
        return sum(d.total_seconds for d in self.discs)

    @property
    def efficiency(self) -> float:
        total_cap = sum(d.config.capacity_seconds for d in self.discs)
        return (self.total_duration / total_cap * 100) if total_cap > 0 else 0.0


# ------------------------------------------------------------------ #
#  Audio scanning                                                      #
# ------------------------------------------------------------------ #


def scan_audio_file(path: Path) -> Optional[Track]:
    """Read metadata and duration from a single audio file using mutagen."""
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return None
    if audio is None or audio.info is None:
        return None
    duration = float(audio.info.length)
    if duration <= 0:
        return None

    title = path.stem
    artist = ""
    album = ""
    tags = audio.tags
    if tags is not None:
        # Try common tag keys across formats
        for key in ("title", "TIT2", "\xa9nam", "TITLE"):
            val = tags.get(key)
            if val:
                title = str(val[0]) if isinstance(val, list) else str(val)
                break
        for key in ("artist", "TPE1", "\xa9ART", "ARTIST"):
            val = tags.get(key)
            if val:
                artist = str(val[0]) if isinstance(val, list) else str(val)
                break
        for key in ("album", "TALB", "\xa9alb", "ALBUM"):
            val = tags.get(key)
            if val:
                album = str(val[0]) if isinstance(val, list) else str(val)
                break

    return Track(
        path=str(path),
        title=title,
        artist=artist,
        album=album,
        duration_seconds=duration,
    )


def scan_audio_files(paths: Sequence[str | Path]) -> tuple[List[Track], List[str]]:
    """Scan multiple paths, returning (tracks, errors)."""
    tracks: List[Track] = []
    errors: List[str] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            children = sorted(
                f for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            )
            for child in children:
                t = scan_audio_file(child)
                if t:
                    tracks.append(t)
                else:
                    errors.append(f"Could not read: {child.name}")
        elif p.is_file():
            if p.suffix.lower() not in AUDIO_EXTENSIONS:
                errors.append(f"Unsupported format: {p.name}")
                continue
            t = scan_audio_file(p)
            if t:
                tracks.append(t)
            else:
                errors.append(f"Could not read: {p.name}")
    return tracks, errors


# ------------------------------------------------------------------ #
#  Splitting algorithms                                                #
# ------------------------------------------------------------------ #


def split_sequential(
    tracks: List[Track], default_config: DiscConfig
) -> SplitResult:
    """Greedy sequential packing — preserves playlist order.

    Fills each disc in order; when the next track would exceed capacity,
    a new disc is started with the same *default_config*.
    """
    if not tracks:
        return SplitResult(discs=[])

    discs: List[Disc] = []
    current = Disc(config=DiscConfig(default_config.disc_size, default_config.mode))

    for track in tracks:
        cap = current.config.capacity_seconds
        track_cost = track.duration_seconds + current.cluster_waste_for(track) + current.track_overhead_seconds
        new_effective = current.effective_seconds + track_cost
        if current.tracks and new_effective > cap:
            discs.append(current)
            current = Disc(
                config=DiscConfig(default_config.disc_size, default_config.mode)
            )
        current.tracks.append(track)

    if current.tracks:
        discs.append(current)

    return SplitResult(discs=discs)


def split_optimized(
    tracks: List[Track], default_config: DiscConfig
) -> SplitResult:
    """First Fit Decreasing — reorders tracks to minimise disc count.

    Sorts tracks longest-first, then assigns each to the first existing
    disc that has room. If none fit, a new disc is created. All discs
    use the same *default_config* (the user can change individual discs
    after the split).
    """
    if not tracks:
        return SplitResult(discs=[])

    sorted_tracks = sorted(tracks, key=lambda t: t.duration_seconds, reverse=True)
    discs: List[Disc] = []

    for track in sorted_tracks:
        placed = False
        for disc in discs:
            track_cost = track.duration_seconds + disc.cluster_waste_for(track) + disc.track_overhead_seconds
            new_effective = disc.effective_seconds + track_cost
            if new_effective <= disc.config.capacity_seconds:
                disc.tracks.append(track)
                placed = True
                break
        if not placed:
            new_disc = Disc(
                config=DiscConfig(default_config.disc_size, default_config.mode)
            )
            new_disc.tracks.append(track)
            discs.append(new_disc)

    return SplitResult(discs=discs)


# ------------------------------------------------------------------ #
#  Smart suggestion engine                                             #
# ------------------------------------------------------------------ #


def find_suggestion(result: SplitResult) -> Optional[str]:
    """Check if switching a disc's config could eliminate the last disc.

    Returns a human-readable tip string, or ``None``.
    """
    if len(result.discs) < 2:
        return None

    last_disc = result.discs[-1]
    overflow = last_disc.effective_seconds  # seconds that need to be absorbed

    for i, disc in enumerate(result.discs[:-1]):
        for size in DISC_SIZES:
            for mode, mult in MODE_MULTIPLIERS.items():
                new_cap = size * 60 * mult
                headroom = new_cap - disc.effective_seconds
                if headroom >= overflow and (size != disc.config.disc_size or mode != disc.config.mode):
                    return (
                        f"Tip: Switching Disc {i + 1} to {size}-min {mode} "
                        f"would absorb all of Disc {len(result.discs)}'s tracks "
                        f"({format_duration(overflow)})."
                    )
    return None


# ------------------------------------------------------------------ #
#  Dark theme stylesheet                                               #
# ------------------------------------------------------------------ #

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
}

QLabel {
    color: #cdd6f4;
}

QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #45475a;
    border-color: #89b4fa;
}
QPushButton:pressed {
    background-color: #585b70;
}

QComboBox {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 4px 8px;
    min-width: 80px;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background-color: #313244;
    color: #cdd6f4;
    selection-background-color: #45475a;
}

QRadioButton {
    color: #cdd6f4;
    spacing: 6px;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
}

QScrollArea {
    border: none;
    background-color: #1e1e2e;
}

QListWidget {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    border-radius: 4px;
    outline: none;
}
QListWidget::item {
    padding: 3px 6px;
}
QListWidget::item:selected {
    background-color: #45475a;
}
QListWidget::item:hover {
    background-color: #313244;
}

QFrame#disc_widget {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
}

QFrame#drop_zone {
    background-color: #181825;
    border: 2px dashed #45475a;
    border-radius: 12px;
}
QFrame#drop_zone[dragOver="true"] {
    border-color: #89b4fa;
    background-color: #1e1e3e;
}
"""


# ------------------------------------------------------------------ #
#  CapacityBar — custom-painted track segments                         #
# ------------------------------------------------------------------ #


class CapacityBar(QWidget):
    """Horizontal bar showing coloured segments for each track's duration."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tracks: List[Track] = []
        self._capacity: float = 74 * 60
        self._cluster_seconds: float = CLUSTER_SP_SECONDS
        self._track_overhead: float = TRACK_METADATA_SP_SECONDS
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(
        self, tracks: List[Track], capacity_seconds: float,
        cluster_seconds: float = CLUSTER_SP_SECONDS,
        track_overhead: float = TRACK_METADATA_SP_SECONDS,
    ) -> None:
        self._tracks = tracks
        self._capacity = max(capacity_seconds, 1.0)
        self._cluster_seconds = cluster_seconds
        self._track_overhead = track_overhead
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # Background (empty space)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#313244"))
        painter.drawRoundedRect(0, 0, w, h, 4, 4)

        cs = self._cluster_seconds
        overhead = self._track_overhead
        total = sum(
            math.ceil(t.duration_seconds / cs) * cs + overhead
            for t in self._tracks
        )

        # Draw each track as a coloured segment (includes cluster waste + overhead)
        x = 0.0
        for i, track in enumerate(self._tracks):
            aligned = math.ceil(track.duration_seconds / cs) * cs + overhead
            frac = aligned / self._capacity
            seg_w = frac * w
            colour = TRACK_COLOURS[i % len(TRACK_COLOURS)]
            if x + seg_w > w:
                # Overflow: draw in red
                colour = "#f38ba8"
            painter.setBrush(QColor(colour))
            painter.drawRect(int(x), 0, max(int(seg_w), 1), h)
            x += seg_w

        # Capacity boundary marker if tracks don't fill the bar
        if total < self._capacity:
            pass  # empty region is already the dark background
        elif total > self._capacity:
            # Draw a red overflow indicator at the capacity boundary
            cap_x = int(w * (self._capacity / max(total, 1)))
            painter.setPen(QPen(QColor("#f38ba8"), 2))
            painter.drawLine(cap_x, 0, cap_x, h)

        painter.end()


# ------------------------------------------------------------------ #
#  DiscWidget — one disc card with config selectors                    #
# ------------------------------------------------------------------ #


class DiscWidget(QFrame):
    """Visual card for a single MiniDisc showing tracks and controls."""

    config_changed = Signal()
    tracks_changed = Signal()

    def __init__(
        self, disc: Disc, disc_index: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("disc_widget")
        self.disc = disc
        self.disc_index = disc_index
        self.setAcceptDrops(True)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)

        # --- Header row ---
        header = QHBoxLayout()
        self._title_label = QLabel()
        self._title_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        header.addWidget(self._title_label)
        header.addStretch()

        self._usage_label = QLabel()
        self._usage_label.setFont(QFont("Segoe UI", 10))
        header.addWidget(self._usage_label)
        layout.addLayout(header)

        # --- Capacity bar ---
        self._capacity_bar = CapacityBar()
        layout.addWidget(self._capacity_bar)

        # --- Track list ---
        self._track_list = QListWidget()
        self._track_list.setDragEnabled(True)
        self._track_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._track_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self._track_list.setMaximumHeight(180)
        layout.addWidget(self._track_list)

        # --- Config selectors ---
        config_row = QHBoxLayout()
        config_row.addWidget(QLabel("Disc:"))
        self._size_combo = QComboBox()
        for s in DISC_SIZES:
            self._size_combo.addItem(f"{s} min", s)
        self._size_combo.setCurrentIndex(
            DISC_SIZES.index(self.disc.config.disc_size)
        )
        self._size_combo.currentIndexChanged.connect(self._on_config_change)
        config_row.addWidget(self._size_combo)

        config_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        for m in MODE_MULTIPLIERS:
            self._mode_combo.addItem(m, m)
        modes = list(MODE_MULTIPLIERS.keys())
        self._mode_combo.setCurrentIndex(modes.index(self.disc.config.mode))
        self._mode_combo.currentIndexChanged.connect(self._on_config_change)
        config_row.addWidget(self._mode_combo)

        config_row.addStretch()
        layout.addLayout(config_row)

    def _on_config_change(self) -> None:
        size = self._size_combo.currentData()
        mode = self._mode_combo.currentData()
        if size is not None:
            self.disc.config.disc_size = size
        if mode is not None:
            self.disc.config.mode = mode
        self.refresh()
        self.config_changed.emit()

    def refresh(self) -> None:
        """Update all visual elements from current disc data."""
        disc = self.disc
        pct = disc.percent_used
        eff_fmt = format_duration(disc.effective_seconds)
        cap_fmt = format_duration(disc.config.capacity_seconds)

        self._title_label.setText(
            f"Disc {self.disc_index + 1}  \u2014  {disc.config.label()}"
        )

        # Colour-coded usage
        if disc.is_over:
            colour = "#f38ba8"   # red — overflow
        elif pct >= 80:
            colour = "#a6e3a1"   # green — well-utilised
        elif pct >= 50:
            colour = "#f9e2af"   # yellow — moderate
        else:
            colour = "#cdd6f4"   # default — low
        self._usage_label.setText(f"{eff_fmt} / {cap_fmt}  ({pct:.1f}%)")
        self._usage_label.setStyleSheet(f"color: {colour};")

        self._capacity_bar.set_data(
            disc.tracks, disc.config.capacity_seconds,
            disc.cluster_seconds, disc.track_overhead_seconds,
        )

        # Refresh track list items
        self._track_list.clear()
        for i, track in enumerate(disc.tracks):
            text = f"{i + 1}. {track.display_name}  —  {format_duration(track.duration_seconds)}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, id(track))
            self._track_list.addItem(item)

    # ---- Drag-and-drop between discs ----

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-md-track-ids"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-md-track-ids"):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData().data("application/x-md-track-ids").data().decode()
        if not data:
            return
        track_ids = [int(x) for x in data.split(",")]
        # Signal to the main window to handle the move
        event.acceptProposedAction()
        window = self.window()
        if isinstance(window, PlaylistSplitterApp):
            window.move_tracks_to_disc(track_ids, self.disc_index)

    def start_drag(self) -> None:
        """Initiate a drag of the selected tracks."""
        selected = self._track_list.selectedItems()
        if not selected:
            return
        ids = []
        for item in selected:
            track_id = item.data(Qt.ItemDataRole.UserRole)
            if track_id is not None:
                ids.append(str(track_id))

        mime = QMimeData()
        mime.setData("application/x-md-track-ids", ",".join(ids).encode())
        drag = QDrag(self._track_list)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)


# ------------------------------------------------------------------ #
#  DropZone — file drop target                                         #
# ------------------------------------------------------------------ #


class DropZone(QFrame):
    """Large drop target for audio files. Click to browse."""

    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(100)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel("\U0001F3B5  Drop music files or folders here\n(or click to browse)")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setFont(QFont("Segoe UI", 13))
        self._label.setStyleSheet("color: #6c7086;")
        layout.addWidget(self._label)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select audio files",
            "",
            "Audio files ("
            + " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
            + ");;All files (*)",
        )
        if paths:
            self.files_dropped.emit(paths)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)

        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        if paths:
            self.files_dropped.emit(paths)


# ------------------------------------------------------------------ #
#  Main window                                                         #
# ------------------------------------------------------------------ #


class PlaylistSplitterApp(QMainWindow):
    """MiniDisc Playlist Splitter — main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MiniDisc Playlist Splitter")
        self.setMinimumSize(820, 640)
        self.resize(920, 720)

        self._tracks: List[Track] = []
        self._disc_widgets: List[DiscWidget] = []
        self._split_result: Optional[SplitResult] = None
        self._undo_stack: List[List[Track]] = []

        self._build_ui()
        self._bind_shortcuts()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        # --- Drop zone ---
        self._drop_zone = DropZone()
        self._drop_zone.files_dropped.connect(self._on_files_dropped)
        root.addWidget(self._drop_zone)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        toolbar.addWidget(QLabel("Default disc:"))
        self._size_combo = QComboBox()
        for s in DISC_SIZES:
            self._size_combo.addItem(f"{s} min", s)
        self._size_combo.setCurrentIndex(1)  # 74 min
        toolbar.addWidget(self._size_combo)

        self._mode_combo = QComboBox()
        for m in MODE_MULTIPLIERS:
            self._mode_combo.addItem(m, m)
        toolbar.addWidget(self._mode_combo)

        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("Strategy:"))
        self._ordered_radio = QRadioButton("Ordered")
        self._optimized_radio = QRadioButton("Optimized")
        self._ordered_radio.setChecked(True)
        strategy_group = QButtonGroup(self)
        strategy_group.addButton(self._ordered_radio)
        strategy_group.addButton(self._optimized_radio)
        toolbar.addWidget(self._ordered_radio)
        toolbar.addWidget(self._optimized_radio)

        toolbar.addStretch()

        self._split_btn = QPushButton("\u25B6  Auto-Split")
        self._split_btn.clicked.connect(self._do_split)
        toolbar.addWidget(self._split_btn)

        self._reset_btn = QPushButton("\u21BA  Reset")
        self._reset_btn.clicked.connect(self._reset)
        toolbar.addWidget(self._reset_btn)

        self._export_btn = QPushButton("\U0001F4CB  Export Listing")
        self._export_btn.clicked.connect(self._export_listing)
        toolbar.addWidget(self._export_btn)

        root.addLayout(toolbar)

        # --- Suggestion banner ---
        self._suggestion_banner = QLabel()
        self._suggestion_banner.setWordWrap(True)
        self._suggestion_banner.setStyleSheet(
            "background-color: #313244; color: #f9e2af; padding: 8px 12px; "
            "border-radius: 6px; font-size: 12px;"
        )
        self._suggestion_banner.hide()
        root.addWidget(self._suggestion_banner)

        # --- Disc scroll area ---
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._disc_container = QWidget()
        self._disc_layout = QVBoxLayout(self._disc_container)
        self._disc_layout.setContentsMargins(0, 0, 0, 0)
        self._disc_layout.setSpacing(10)
        self._disc_layout.addStretch()
        self._scroll.setWidget(self._disc_container)
        root.addWidget(self._scroll, stretch=1)

        # --- Summary bar ---
        self._summary_label = QLabel()
        self._summary_label.setFont(QFont("Segoe UI", 10))
        self._summary_label.setStyleSheet("color: #89b4fa; padding: 4px;")
        root.addWidget(self._summary_label)

    def _bind_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+O"), self, self._browse_files)
        QShortcut(QKeySequence("Ctrl+S"), self, self._export_listing)
        QShortcut(QKeySequence.StandardKey.Delete, self, self._delete_selected)
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo)

    # ---- File handling ----

    def _browse_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select audio files",
            "",
            "Audio files ("
            + " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
            + ");;All files (*)",
        )
        if paths:
            self._on_files_dropped(paths)

    def _on_files_dropped(self, paths: list) -> None:
        tracks, errors = scan_audio_files(paths)
        if not tracks and errors:
            QMessageBox.warning(self, "No tracks loaded", "\n".join(errors[:15]))
            return
        self._save_undo()
        self._tracks.extend(tracks)
        self._drop_zone._label.setText(
            f"\U0001F3B5  {len(self._tracks)} track(s) loaded  "
            f"({format_duration(sum(t.duration_seconds for t in self._tracks))})\n"
            "Drop more files, or click to browse"
        )
        if errors:
            QMessageBox.warning(
                self,
                "Some files skipped",
                "\n".join(errors[:15])
                + ("\n\u2026and more." if len(errors) > 15 else ""),
            )

    # ---- Split logic ----

    def _default_config(self) -> DiscConfig:
        return DiscConfig(
            disc_size=self._size_combo.currentData() or 74,
            mode=self._mode_combo.currentData() or "SP",
        )

    def _do_split(self) -> None:
        if not self._tracks:
            QMessageBox.information(self, "No tracks", "Drop audio files first.")
            return

        # Check for tracks longer than max possible disc
        max_cap = max(
            s * 60 * m for s in DISC_SIZES for m in MODE_MULTIPLIERS.values()
        )
        oversized = [t for t in self._tracks if t.duration_seconds > max_cap]
        if oversized:
            names = "\n".join(
                f"  \u2022 {t.display_name} ({format_duration(t.duration_seconds)})"
                for t in oversized[:5]
            )
            QMessageBox.warning(
                self,
                "Tracks exceed maximum disc capacity",
                f"The following tracks are longer than the largest possible "
                f"disc ({format_duration(max_cap)}):\n\n{names}\n\n"
                f"They will each occupy their own disc and will be over capacity.",
            )

        config = self._default_config()
        if self._ordered_radio.isChecked():
            result = split_sequential(self._tracks, config)
        else:
            result = split_optimized(self._tracks, config)

        self._split_result = result
        self._populate_discs(result)
        self._update_summary()

        # Smart suggestion
        tip = find_suggestion(result)
        if tip:
            self._suggestion_banner.setText(tip)
            self._suggestion_banner.show()
        else:
            self._suggestion_banner.hide()

    def _populate_discs(self, result: SplitResult) -> None:
        # Clear existing disc widgets
        for dw in self._disc_widgets:
            self._disc_layout.removeWidget(dw)
            dw.deleteLater()
        self._disc_widgets.clear()

        for i, disc in enumerate(result.discs):
            dw = DiscWidget(disc, i)
            dw.config_changed.connect(self._on_disc_config_changed)
            dw.tracks_changed.connect(self._update_summary)

            # Enable drag initiation from the track list
            dw._track_list.setDragEnabled(True)
            dw._track_list.model().rowsInserted.connect(
                lambda *_a, w=dw: w.start_drag()
            )
            # Use the list's startDrag instead of model signal
            dw._track_list.setDragDropMode(
                QListWidget.DragDropMode.DragOnly
            )

            self._disc_layout.insertWidget(
                self._disc_layout.count() - 1, dw  # before the stretch
            )
            self._disc_widgets.append(dw)

    def _on_disc_config_changed(self) -> None:
        self._update_summary()
        # Re-check suggestion
        if self._split_result:
            tip = find_suggestion(self._split_result)
            if tip:
                self._suggestion_banner.setText(tip)
                self._suggestion_banner.show()
            else:
                self._suggestion_banner.hide()

    def _update_summary(self) -> None:
        if not self._disc_widgets:
            self._summary_label.setText("")
            return

        discs = [dw.disc for dw in self._disc_widgets]
        total_tracks = sum(len(d.tracks) for d in discs)
        total_eff = sum(d.effective_seconds for d in discs)
        total_cap = sum(d.config.capacity_seconds for d in discs)
        efficiency = (total_eff / total_cap * 100) if total_cap > 0 else 0.0

        # Shopping list
        from collections import Counter

        shopping: Counter[str] = Counter()
        for d in discs:
            shopping[f"{d.config.disc_size}-min"] += 1
        shopping_str = ", ".join(
            f"{count}\u00d7 {label}" for label, count in sorted(shopping.items())
        )

        self._summary_label.setText(
            f"{len(discs)} disc(s)  \u2502  "
            f"{total_tracks} track(s)  \u2502  "
            f"{format_duration(total_eff)} total  \u2502  "
            f"{efficiency:.1f}% efficiency  \u2502  "
            f"Shopping list: {shopping_str}"
        )

    # ---- Track movement between discs ----

    def move_tracks_to_disc(self, track_ids: list[int], target_index: int) -> None:
        """Move tracks (by Python id) from their current disc to *target_index*."""
        self._save_undo()
        target_disc = self._disc_widgets[target_index].disc

        moved: List[Track] = []
        for dw in self._disc_widgets:
            remaining: List[Track] = []
            for t in dw.disc.tracks:
                if id(t) in track_ids:
                    moved.append(t)
                else:
                    remaining.append(t)
            dw.disc.tracks = remaining

        target_disc.tracks.extend(moved)

        for dw in self._disc_widgets:
            dw.refresh()
        self._update_summary()

    def _delete_selected(self) -> None:
        """Delete selected tracks from whichever disc they're on."""
        for dw in self._disc_widgets:
            selected = dw._track_list.selectedItems()
            if not selected:
                continue
            self._save_undo()
            ids_to_remove = {
                item.data(Qt.ItemDataRole.UserRole) for item in selected
            }
            dw.disc.tracks = [
                t for t in dw.disc.tracks if id(t) not in ids_to_remove
            ]
            dw.refresh()
        self._update_summary()

    # ---- Export ----

    def _export_listing(self) -> None:
        if not self._disc_widgets:
            QMessageBox.information(
                self, "Nothing to export", "Run Auto-Split first."
            )
            return

        lines: List[str] = []
        lines.append("MiniDisc Playlist Split")
        lines.append("=" * 40)

        for dw in self._disc_widgets:
            disc = dw.disc
            pct = disc.percent_used
            lines.append("")
            lines.append(
                f"Disc {dw.disc_index + 1}  —  {disc.config.label()}  "
                f"({format_duration(disc.total_seconds)} / "
                f"{format_duration(disc.config.capacity_seconds)}, "
                f"{pct:.1f}%)"
            )
            lines.append("-" * 40)
            for i, track in enumerate(disc.tracks):
                lines.append(
                    f"  {i + 1:2d}. {track.display_name}"
                    f"  [{format_duration(track.duration_seconds)}]"
                )

        lines.append("")
        lines.append("=" * 40)
        lines.append(self._summary_label.text())
        lines.append("")

        text = "\n".join(lines)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
        QMessageBox.information(
            self,
            "Exported",
            "Track listing copied to clipboard!",
        )

    # ---- Reset / Undo ----

    def _reset(self) -> None:
        self._save_undo()
        self._tracks.clear()
        self._split_result = None
        self._suggestion_banner.hide()
        for dw in self._disc_widgets:
            self._disc_layout.removeWidget(dw)
            dw.deleteLater()
        self._disc_widgets.clear()
        self._update_summary()
        self._drop_zone._label.setText(
            "\U0001F3B5  Drop music files or folders here\n(or click to browse)"
        )

    def _save_undo(self) -> None:
        self._undo_stack.append(list(self._tracks))
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        self._tracks = self._undo_stack.pop()
        # Re-split with current settings if we had a split
        if self._split_result:
            self._do_split()


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    window = PlaylistSplitterApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
