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
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from mutagen import File as MutagenFile  # type: ignore[import-untyped]

from PySide6.QtCore import (
    QMimeData,
    QPoint,
    QSize,
    QThread,
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
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    from resize_album_to_fit_md import (
        OUTPUT_FORMATS as RESIZE_OUTPUT_FORMATS,
        calculate_speed_factor,
        check_tool as _check_ffmpeg,
        compute_playlist_resize_factor,
        format_duration_short as _fmt_short,
        resize_tracks,
    )
    _RESIZE_AVAILABLE = _check_ffmpeg("ffmpeg") and _check_ffmpeg("ffprobe")
except ImportError:
    _RESIZE_AVAILABLE = False

    def compute_playlist_resize_factor(  # type: ignore[misc]
        total_resize_seconds: float,
        total_fixed_seconds: float,
        target_capacity_seconds: float,
    ) -> "Optional[float]":
        available = target_capacity_seconds - total_fixed_seconds
        if available <= 0:
            return None
        if total_resize_seconds <= 0:
            return None
        return total_resize_seconds / available

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
#  ResizeToFitDialog — configure and run per-disc audio resize         #
# ------------------------------------------------------------------ #


class ResizeToFitDialog(QDialog):
    """Dialog that lets the user configure and run a 'Resize to Fit' operation.

    Opens when the user clicks the 'Resize to Fit' button on an over-capacity
    disc.  After the user confirms, audio files are processed by ffmpeg via
    :func:`resize_tracks` (from ``resize_album_to_fit_md``).
    """

    def __init__(self, disc: "Disc", disc_index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.disc = disc
        self.disc_index = disc_index
        self.setWindowTitle(f"Resize Disc {disc_index + 1} to Fit")
        self.setMinimumWidth(460)
        self._output_folder: Optional[str] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- Info label ---
        cap_fmt = format_duration(self.disc.config.capacity_seconds)
        eff_fmt = format_duration(self.disc.effective_seconds)
        over_by = self.disc.effective_seconds - self.disc.config.capacity_seconds
        info = QLabel(
            f"<b>Disc {self.disc_index + 1}</b> ({self.disc.config.label()}) is "
            f"<span style='color:#f38ba8;'>over capacity</span> by "
            f"<b>{format_duration(over_by)}</b> "
            f"({eff_fmt} / {cap_fmt}).<br><br>"
            "Speed up all tracks on this disc proportionally so they fit "
            "within the disc's capacity."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # --- Speed factor preview ---
        total_audio = self.disc.total_seconds
        target = self.disc.config.capacity_seconds
        factor = total_audio / target if target > 0 else 1.0
        pct = (factor - 1) * 100
        self._factor_label = QLabel(
            f"Required speed factor: <b>{factor:.4f}×</b> ({pct:.2f}% faster)"
        )
        self._factor_label.setStyleSheet("color: #f9e2af;")
        layout.addWidget(self._factor_label)

        # --- Options group ---
        group = QGroupBox("Options")
        group_layout = QVBoxLayout(group)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Speed  (pitch rises with tempo)", "Speed")
        self._mode_combo.addItem("TimeStretch  (pitch preserved, needs librubberband)", "TimeStretch")
        mode_row.addWidget(self._mode_combo, stretch=1)
        group_layout.addLayout(mode_row)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Output format:"))
        self._fmt_combo = QComboBox()
        if _RESIZE_AVAILABLE:
            for fmt in RESIZE_OUTPUT_FORMATS:
                self._fmt_combo.addItem(fmt, fmt)
        else:
            for fmt in ("FLAC", "WAV", "AIFF", "ALAC", "APE", "WavPack", "TTA"):
                self._fmt_combo.addItem(fmt, fmt)
        fmt_row.addWidget(self._fmt_combo, stretch=1)
        group_layout.addLayout(fmt_row)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Output folder:"))
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("(required)")
        self._folder_edit.setReadOnly(True)
        folder_row.addWidget(self._folder_edit, stretch=1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse_btn)
        group_layout.addLayout(folder_row)

        self._reload_check = QCheckBox("Reload resized tracks into this disc when done")
        self._reload_check.setChecked(True)
        group_layout.addWidget(self._reload_check)

        layout.addWidget(group)

        # --- Warning if ffmpeg unavailable ---
        if not _RESIZE_AVAILABLE:
            warn = QLabel(
                "⚠  <b>ffmpeg / ffprobe not found in PATH.</b>  "
                "Install ffmpeg to enable resizing."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #f38ba8; background: #313244; padding: 6px; border-radius: 4px;")
            layout.addWidget(warn)

        # --- Dialog buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn:
            ok_btn.setText("Resize")
            ok_btn.setEnabled(_RESIZE_AVAILABLE)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder for resized tracks"
        )
        if folder:
            self._output_folder = folder
            self._folder_edit.setText(folder)

    def _on_accept(self) -> None:
        if not self._output_folder:
            QMessageBox.warning(
                self,
                "No output folder",
                "Please select an output folder before resizing.",
            )
            return
        self.accept()

    # ---- Public accessors ----

    def selected_mode(self) -> str:
        return self._mode_combo.currentData() or "Speed"

    def selected_format(self) -> str:
        return self._fmt_combo.currentData() or "FLAC"

    def output_folder(self) -> str:
        return self._output_folder or ""

    def reload_after(self) -> bool:
        return self._reload_check.isChecked()


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
#  ReorderableTrackList — QListWidget with internal reorder support     #
# ------------------------------------------------------------------ #


class ReorderableTrackList(QListWidget):
    """QListWidget that supports both intra-disc reordering and inter-disc drag.

    Dragging always uses the ``application/x-md-track-ids`` MIME type.
    Drops within the same list reorder tracks; drops on a different
    *DiscWidget* move tracks between discs (handled by ``DiscWidget.dropEvent``).
    """

    rows_reordered = Signal(list, int)  # (track_ids, drop_row)

    def startDrag(self, supportedActions) -> None:  # noqa: N802
        selected = self.selectedItems()
        if not selected:
            return
        ids = []
        for item in selected:
            track_id = item.data(Qt.ItemDataRole.UserRole)
            if track_id is not None:
                ids.append(str(track_id))
        if not ids:
            return

        mime = QMimeData()
        mime.setData("application/x-md-track-ids", ",".join(ids).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-md-track-ids"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat("application/x-md-track-ids"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat("application/x-md-track-ids"):
            super().dropEvent(event)
            return

        data = event.mimeData().data("application/x-md-track-ids").data().decode()
        if not data:
            return
        drop_track_ids = [int(x) for x in data.split(",")]

        # Check if the dropped tracks all belong to this list (intra-disc reorder)
        own_ids = set()
        for row in range(self.count()):
            tid = self.item(row).data(Qt.ItemDataRole.UserRole)
            if tid is not None:
                own_ids.add(tid)

        if not set(drop_track_ids).issubset(own_ids):
            # Inter-disc move — let DiscWidget.dropEvent handle it
            event.ignore()
            return

        # Intra-disc reorder: figure out the target row from the drop position
        target_item = self.itemAt(event.position().toPoint())
        drop_row = self.row(target_item) if target_item else self.count()

        event.acceptProposedAction()
        self.rows_reordered.emit(drop_track_ids, drop_row)


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

        # --- Track list (supports intra-disc reorder + inter-disc drag) ---
        self._track_list = ReorderableTrackList()
        self._track_list.setDragEnabled(True)
        self._track_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._track_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._track_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self._track_list.setMaximumHeight(180)
        self._track_list.rows_reordered.connect(self._on_rows_reordered)
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

        # --- Resize to Fit button (shown only when disc is over capacity) ---
        self._resize_btn = QPushButton("\u26A1  Resize to Fit")
        self._resize_btn.setToolTip(
            "Speed up all tracks on this disc proportionally so they fit "
            "within its capacity (requires ffmpeg)."
        )
        self._resize_btn.setStyleSheet(
            "QPushButton { background-color: #45475a; color: #f38ba8; "
            "border: 1px solid #f38ba8; border-radius: 6px; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #585b70; }"
        )
        self._resize_btn.clicked.connect(self._on_resize_clicked)
        self._resize_btn.setVisible(False)
        config_row.addWidget(self._resize_btn)

        layout.addLayout(config_row)

    def _on_rows_reordered(self, track_ids: list[int], drop_row: int) -> None:
        """Reorder disc.tracks after the user drags items within the list."""
        id_to_track = {id(t): t for t in self.disc.tracks}
        moved_set = set(track_ids)
        moved = [id_to_track[tid] for tid in track_ids if tid in id_to_track]
        remaining = [t for t in self.disc.tracks if id(t) not in moved_set]

        # Insert the moved tracks at the drop position within the remaining list
        insert_at = min(drop_row, len(remaining))
        self.disc.tracks = remaining[:insert_at] + moved + remaining[insert_at:]
        self.refresh()
        self.tracks_changed.emit()

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

        # Show Resize-to-Fit button only when disc is over capacity
        self._resize_btn.setVisible(disc.is_over and _RESIZE_AVAILABLE)

    # ---- Resize to Fit ----

    def _on_resize_clicked(self) -> None:
        """Open the Resize-to-Fit dialog and run the resize if confirmed."""
        dialog = ResizeToFitDialog(self.disc, self.disc_index, parent=self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        base_output = dialog.output_folder()
        mode = dialog.selected_mode()
        fmt = dialog.selected_format()
        do_reload = dialog.reload_after()

        # Gather the source files from this disc's tracks
        input_files = [Path(t.path) for t in self.disc.tracks]
        if not input_files:
            return

        # Calculate the speed factor from raw audio seconds vs disc capacity
        total_audio = self.disc.total_seconds
        target = self.disc.config.capacity_seconds
        if target <= 0 or total_audio <= target:
            return
        speed_factor = total_audio / target

        # Create disc subfolder (e.g., "Disc 01 of 03")
        window = self.window()
        total_discs = (
            len(window._disc_widgets) if isinstance(window, PlaylistSplitterApp) else 1
        )
        disc_num = self.disc_index + 1
        pad = len(str(total_discs))
        subfolder = Path(base_output) / f"Disc {disc_num:0{pad}d} of {total_discs:0{pad}d}"
        subfolder.mkdir(parents=True, exist_ok=True)
        output_folder = str(subfolder)

        # Run resize in a background thread to keep the UI responsive
        results_holder: list = [None]
        done_event = threading.Event()

        def _worker() -> None:
            try:
                results_holder[0] = resize_tracks(
                    input_files,
                    output_folder,
                    speed_factor,
                    mode=mode,
                    output_format=fmt,
                    quiet=True,
                )
            except Exception as exc:  # noqa: BLE001 — intentionally broad; background worker must not crash silently
                results_holder[0] = exc
            finally:
                done_event.set()

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()

        progress = QProgressDialog(
            f"Resizing {len(input_files)} track(s) for Disc {self.disc_index + 1}…",
            None,  # no cancel button
            0, 0,
            self.window(),
        )
        progress.setWindowTitle("Resizing Tracks")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()

        while not done_event.wait(timeout=0.05):
            QApplication.processEvents()

        progress.close()

        raw = results_holder[0]
        if isinstance(raw, Exception):
            QMessageBox.critical(
                self.window(),
                "Resize failed",
                f"An error occurred during resizing:\n{raw}",
            )
            return

        results = raw  # list of (output_path, duration_or_None)
        failed = [reason for reason, d in results if d is None]
        if failed:
            QMessageBox.warning(
                self.window(),
                "Some tracks failed",
                "The following tracks could not be resized:\n"
                + "\n".join(f"  • {reason}" for reason in failed),
            )

        successful_paths = [p for p, d in results if d is not None]
        if not successful_paths:
            return

        if do_reload:
            window = self.window()
            if isinstance(window, PlaylistSplitterApp):
                window.reload_resized_disc(self.disc_index, successful_paths)

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
        self._natural_disc_count: int = 0
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

        # --- Target disc row (visible when ≥2 discs and ffmpeg available) ---
        self._target_row_widget = QWidget()
        target_row = QHBoxLayout(self._target_row_widget)
        target_row.setContentsMargins(0, 4, 0, 4)

        target_row.addWidget(QLabel("Target disc count:"))
        self._target_spin = QSpinBox()
        self._target_spin.setRange(1, 1)
        self._target_spin.setValue(1)
        self._target_spin.setToolTip(
            "How many discs the playlist should fit on after resizing."
        )
        self._target_spin.valueChanged.connect(self._on_target_changed)
        target_row.addWidget(self._target_spin)
        target_row.addSpacing(12)
        self._factor_label = QLabel()
        self._factor_label.setWordWrap(True)
        target_row.addWidget(self._factor_label, stretch=1)

        self._target_row_widget.hide()
        root.addWidget(self._target_row_widget)

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

        # --- Resize panel (shown when target disc count < current disc count) ---
        self._resize_panel = QFrame()
        self._resize_panel.setObjectName("resize_panel")
        self._resize_panel.setStyleSheet(
            "#resize_panel { background-color: #313244; border-radius: 8px; }"
        )
        rp_layout = QVBoxLayout(self._resize_panel)
        rp_layout.setContentsMargins(14, 10, 14, 10)
        rp_layout.setSpacing(6)

        # Options row: mode, format, output folder
        opts_row = QHBoxLayout()
        opts_row.setSpacing(8)

        opts_row.addWidget(QLabel("Mode:"))
        self._resize_mode_combo = QComboBox()
        self._resize_mode_combo.addItem("Speed  (pitch rises with tempo)", "Speed")
        self._resize_mode_combo.addItem(
            "TimeStretch  (pitch preserved, needs librubberband)", "TimeStretch"
        )
        opts_row.addWidget(self._resize_mode_combo)

        opts_row.addWidget(QLabel("Output format:"))
        self._resize_fmt_combo = QComboBox()
        if _RESIZE_AVAILABLE:
            for fmt in RESIZE_OUTPUT_FORMATS:
                self._resize_fmt_combo.addItem(fmt, fmt)
        else:
            for fmt in ("FLAC", "WAV", "AIFF", "ALAC", "APE", "WavPack", "TTA"):
                self._resize_fmt_combo.addItem(fmt, fmt)
        opts_row.addWidget(self._resize_fmt_combo)

        opts_row.addWidget(QLabel("Output folder:"))
        self._resize_folder_edit = QLineEdit()
        self._resize_folder_edit.setPlaceholderText("(required)")
        self._resize_folder_edit.setReadOnly(True)
        opts_row.addWidget(self._resize_folder_edit, stretch=1)
        browse_btn = QPushButton("Browse\u2026")
        browse_btn.clicked.connect(self._browse_resize_folder)
        opts_row.addWidget(browse_btn)

        rp_layout.addLayout(opts_row)

        # Reload checkbox + Resize button row
        action_row = QHBoxLayout()
        self._reload_check = QCheckBox("Reload resized tracks into their discs when done")
        self._reload_check.setChecked(True)
        action_row.addWidget(self._reload_check)
        action_row.addStretch()

        self._resize_playlist_btn = QPushButton("\u26A1  Resize Playlist")
        self._resize_playlist_btn.setToolTip(
            "Speed up all tracks proportionally so the playlist fits on the "
            "target number of discs (requires ffmpeg)."
        )
        self._resize_playlist_btn.setStyleSheet(
            "QPushButton { background-color: #45475a; color: #f9e2af; "
            "border: 1px solid #f9e2af; border-radius: 6px; padding: 6px 14px; "
            "font-weight: bold; }"
            "QPushButton:hover { background-color: #585b70; }"
            "QPushButton:disabled { color: #585b70; border-color: #585b70; }"
        )
        self._resize_playlist_btn.clicked.connect(self._on_resize_playlist_clicked)
        action_row.addWidget(self._resize_playlist_btn)

        rp_layout.addLayout(action_row)

        self._resize_panel.hide()
        self._resize_output_folder: Optional[str] = None
        root.addWidget(self._resize_panel)

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
        self._natural_disc_count = len(result.discs)
        self._populate_discs(result)

        # Reset target spinner to the natural disc count after a fresh split
        n = self._natural_disc_count
        self._target_spin.blockSignals(True)
        self._target_spin.setRange(1, n)
        self._target_spin.setValue(n)
        self._target_spin.blockSignals(False)

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

            # Drag-drop is configured in DiscWidget._build_ui;
            # no additional setup needed here.

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
            self._target_row_widget.hide()
            self._resize_panel.hide()
            self._target_spin.blockSignals(True)
            self._target_spin.setRange(1, 1)
            self._target_spin.setValue(1)
            self._target_spin.blockSignals(False)
            self._natural_disc_count = 0
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

        # Use the natural disc count for the spinner range so the user can
        # go back to the original split after previewing a lower target.
        nat = self._natural_disc_count
        self._target_spin.blockSignals(True)
        self._target_spin.setRange(1, max(nat, 1))
        if self._target_spin.value() > nat or self._target_spin.value() < 1:
            self._target_spin.setValue(nat)
        self._target_spin.blockSignals(False)

        # Show target row when ≥2 natural discs and ffmpeg available
        show_target = _RESIZE_AVAILABLE and nat >= 2
        self._target_row_widget.setVisible(show_target)

        # Show resize options panel only when target < natural
        show_resize = show_target and self._target_spin.value() < nat
        self._resize_panel.setVisible(show_resize)
        if show_target:
            self._refresh_factor_label()

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

    # ---- Cross-disc "Resize Playlist" ----

    def _on_target_changed(self, value: int) -> None:
        """React to the user changing the target disc spinner."""
        nat = self._natural_disc_count
        if value < nat:
            self._regroup_for_target(value)
        elif value == nat and self._split_result:
            # Restore the original split layout
            self._populate_discs(self._split_result)
        self._update_summary()

    def _regroup_for_target(self, target: int) -> None:
        """Re-distribute all tracks across *target* discs using predicted
        post-resize durations for bin-packing, then update the disc view."""
        all_tracks = [t for dw in self._disc_widgets for t in dw.disc.tracks]
        if not all_tracks:
            return

        cfg = self._default_config()
        total_raw = sum(t.duration_seconds for t in all_tracks)

        # Compute speed factor accounting for overhead
        temp_disc = Disc(config=cfg)
        overhead = sum(
            temp_disc.cluster_waste_for(t) + temp_disc.track_overhead_seconds
            for t in all_tracks
        )
        available = cfg.capacity_seconds * target - overhead
        speed_factor = total_raw / available if available > 0 else 2.0
        if speed_factor < 1.0:
            speed_factor = 1.0

        # Bin-pack using predicted durations into target discs
        discs = [Disc(config=cfg) for _ in range(target)]
        bin_idx = 0
        for track in all_tracks:
            predicted_dur = track.duration_seconds / speed_factor
            predicted_track = Track(
                path=track.path, title=track.title, artist=track.artist,
                album=track.album, duration_seconds=predicted_dur,
            )
            td = discs[bin_idx]
            track_cost = (
                predicted_dur
                + td.cluster_waste_for(predicted_track)
                + td.track_overhead_seconds
            )
            cap = td.config.capacity_seconds
            if td.tracks and bin_idx + 1 < target:
                # Check if this track would overflow the current bin
                current_used = sum(
                    t.duration_seconds / speed_factor
                    + td.cluster_waste_for(t)
                    + td.track_overhead_seconds
                    for t in td.tracks
                )
                if current_used + track_cost > cap:
                    bin_idx += 1
                    td = discs[bin_idx]

            td.tracks.append(track)

        # Remove empty discs (shouldn't happen but be safe)
        filled = [d for d in discs if d.tracks]
        result = SplitResult(discs=filled)
        self._populate_discs(result)

    def _browse_resize_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self._resize_output_folder = folder
            self._resize_folder_edit.setText(folder)

    def _refresh_factor_label(self) -> None:
        """Compute and display the speed factor needed for the current target."""
        target = self._target_spin.value()
        nat = self._natural_disc_count
        if target >= nat or not self._disc_widgets:
            self._factor_label.setText("")
            return

        all_tracks = [t for dw in self._disc_widgets for t in dw.disc.tracks]
        cfg = self._default_config()
        total_raw = sum(t.duration_seconds for t in all_tracks)
        total_cap = cfg.capacity_seconds * target

        # Account for cluster waste + metadata overhead
        temp_disc = Disc(config=cfg)
        overhead = sum(
            temp_disc.cluster_waste_for(t) + temp_disc.track_overhead_seconds
            for t in all_tracks
        )
        available = total_cap - overhead
        if available <= 0:
            self._factor_label.setText(
                "<span style='color:#f38ba8;'>Cannot fit — overhead alone exceeds capacity.</span>"
            )
            return

        speed_factor = total_raw / available
        if speed_factor <= 1.0:
            self._factor_label.setText(
                "<span style='color:#a6e3a1;'>Tracks already fit on "
                f"{target} disc(s) without resizing.</span>"
            )
            return

        pct = (speed_factor - 1) * 100
        self._factor_label.setText(
            f"<b>{speed_factor:.4f}\u00d7</b> speed "
            f"(+{pct:.1f}% faster)"
        )

    def _on_resize_playlist_clicked(self) -> None:
        """Execute the resize using the inline panel settings."""
        if self._natural_disc_count < 2:
            return

        target_count = self._target_spin.value()
        nat = self._natural_disc_count
        if target_count >= nat:
            return

        all_tracks = [t for dw in self._disc_widgets for t in dw.disc.tracks]
        if not all_tracks:
            return

        cfg = self._default_config()
        total_raw = sum(t.duration_seconds for t in all_tracks)
        total_cap = cfg.capacity_seconds * target_count

        temp_disc = Disc(config=cfg)
        overhead = sum(
            temp_disc.cluster_waste_for(t) + temp_disc.track_overhead_seconds
            for t in all_tracks
        )
        available = total_cap - overhead
        if available <= 0:
            QMessageBox.warning(self, "Cannot fit", "Overhead alone exceeds capacity.")
            return
        speed_factor = total_raw / available
        if speed_factor <= 1.0:
            QMessageBox.information(
                self, "No resize needed",
                f"Tracks already fit on {target_count} disc(s)."
            )
            return

        base_output = self._resize_output_folder
        if not base_output:
            QMessageBox.warning(self, "No output folder", "Select an output folder first.")
            return

        mode = self._resize_mode_combo.currentData()
        fmt = self._resize_fmt_combo.currentData()
        do_reload = self._reload_check.isChecked()

        # Build target disc configs (all use default config)
        target_configs = [cfg] * target_count

        # Predict post-resize durations and sequentially assign tracks to
        # target discs so the output subfolders match the target count.
        pad = len(str(target_count))
        disc_bins: list[tuple[Path, list[Path], float]] = []  # (subfolder, files, used_seconds)
        for i in range(target_count):
            sub = Path(base_output) / f"Disc {i + 1:0{pad}d} of {target_count:0{pad}d}"
            disc_bins.append((sub, [], 0.0))

        # Build a temporary Disc per target slot to reuse cluster/overhead math
        temp_discs = [Disc(config=cfg) for cfg in target_configs]
        bin_idx = 0
        for track in all_tracks:
            predicted_dur = track.duration_seconds / speed_factor
            predicted_track = Track(
                path=track.path, title=track.title, artist=track.artist,
                album=track.album, duration_seconds=predicted_dur,
            )
            td = temp_discs[bin_idx]
            track_cost = (
                predicted_dur
                + td.cluster_waste_for(predicted_track)
                + td.track_overhead_seconds
            )
            cap = td.config.capacity_seconds
            # Move to next disc if this one would overflow (and there is a next)
            if td.tracks and disc_bins[bin_idx][2] + track_cost > cap and bin_idx + 1 < target_count:
                bin_idx += 1
                td = temp_discs[bin_idx]
                track_cost = (
                    predicted_dur
                    + td.cluster_waste_for(predicted_track)
                    + td.track_overhead_seconds
                )
            td.tracks.append(predicted_track)
            sub, files, used = disc_bins[bin_idx]
            files.append(Path(track.path))
            disc_bins[bin_idx] = (sub, files, used + track_cost)

        # Create subfolders and drop empties
        disc_groups: list[tuple[Path, list[Path]]] = []
        for sub, files, _ in disc_bins:
            if files:
                sub.mkdir(parents=True, exist_ok=True)
                disc_groups.append((sub, files))

        all_input_files = [f for _, files in disc_groups for f in files]
        if not all_input_files:
            return

        results_holder: list = [None]
        done_event = threading.Event()

        def _worker() -> None:
            try:
                combined: list[tuple[str, float | None]] = []
                for subfolder, files in disc_groups:
                    combined.extend(
                        resize_tracks(
                            files,
                            str(subfolder),
                            speed_factor,
                            mode=mode,
                            output_format=fmt,
                            quiet=True,
                        )
                    )
                results_holder[0] = combined
            except Exception as exc:  # noqa: BLE001
                # Catching Exception (not BaseException) is intentional: KeyboardInterrupt
                # and SystemExit are BaseException but not Exception, so they won't be
                # silently swallowed. Any other unexpected error from resize_tracks is
                # captured here so the background thread can communicate it to the UI.
                results_holder[0] = exc
            finally:
                done_event.set()

        threading.Thread(target=_worker, daemon=True).start()

        progress = QProgressDialog(
            f"Resizing {len(all_input_files)} track(s)…",
            None,
            0, 0,
            self,
        )
        progress.setWindowTitle("Resizing Playlist Tracks")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()

        while not done_event.wait(timeout=0.05):
            QApplication.processEvents()

        progress.close()

        raw = results_holder[0]
        if isinstance(raw, Exception):
            QMessageBox.critical(
                self,
                "Resize failed",
                f"An error occurred during resizing:\n{raw}",
            )
            return

        results = raw  # list of (output_path, duration_or_None)
        failed = [reason for reason, d in results if d is None]
        if failed:
            QMessageBox.warning(
                self,
                "Some tracks failed",
                "The following tracks could not be resized:\n"
                + "\n".join(f"  • {reason}" for reason in failed),
            )

        if do_reload:
            self._reload_resized_playlist(all_tracks, results)

    def _reload_resized_playlist(
        self,
        original_tracks: "List[Track]",
        resize_results: "List[tuple[str, Optional[float]]]",
    ) -> None:
        """Replace resized tracks in-place across all disc widgets.

        Builds a mapping ``original_path → new_path`` from the resize results
        and re-scans the new files to get accurate durations, then substitutes
        each old track with its resized counterpart in whichever disc it lives.

        Parameters
        ----------
        original_tracks: The Track objects that were passed to ffmpeg.
        resize_results:  Pairs of ``(output_path, duration)`` in the same order
                         as *original_tracks*.  ``duration`` is ``None`` on failure.
        """
        # Build old path → new path for successful encodes
        path_map: dict[str, str] = {}
        new_paths: List[str] = []
        for orig, (new_path, dur) in zip(original_tracks, resize_results):
            if dur is not None:
                path_map[orig.path] = new_path
                new_paths.append(new_path)

        if not new_paths:
            return

        # Scan the new files to get Track objects with correct metadata/duration
        new_track_objs, errors = scan_audio_files(new_paths)
        if errors:
            QMessageBox.warning(
                self,
                "Some resized tracks could not be loaded",
                "\n".join(errors[:15])
                + ("\n\u2026and more." if len(errors) > 15 else ""),
            )

        # Index new tracks by their file path
        new_by_path: dict[str, Track] = {t.path: t for t in new_track_objs}

        self._save_undo()
        for dw in self._disc_widgets:
            new_list: List[Track] = []
            for t in dw.disc.tracks:
                if t.path in path_map:
                    resized = new_by_path.get(path_map[t.path])
                    new_list.append(resized if resized is not None else t)
                else:
                    new_list.append(t)
            dw.disc.tracks = new_list
            dw.refresh()

        # Keep the global track list consistent
        self._tracks = [t for dw in self._disc_widgets for t in dw.disc.tracks]

        # After resize+reload the tracks have new durations; treat the
        # current disc layout as the new natural state.
        self._natural_disc_count = len(self._disc_widgets)
        cfg = self._default_config()
        if self._ordered_radio.isChecked():
            result = split_sequential(self._tracks, cfg)
        else:
            result = split_optimized(self._tracks, cfg)
        self._split_result = result
        self._natural_disc_count = len(result.discs)
        self._populate_discs(result)

        self._target_spin.blockSignals(True)
        self._target_spin.setRange(1, self._natural_disc_count)
        self._target_spin.setValue(self._natural_disc_count)
        self._target_spin.blockSignals(False)

        self._update_summary()

        n_ok = len(new_paths)
        QMessageBox.information(
            self,
            "Reload complete",
            f"{n_ok} track(s) have been replaced with their resized versions.",
        )

    def reload_resized_disc(self, disc_index: int, new_paths: List[str]) -> None:
        """Replace the tracks on *disc_index* with the resized files at *new_paths*.

        Called after a successful 'Resize to Fit' operation when the user has
        opted to reload the resized tracks into the disc.
        """
        if disc_index >= len(self._disc_widgets):
            return

        new_tracks, errors = scan_audio_files(new_paths)
        if errors:
            QMessageBox.warning(
                self,
                "Some resized tracks could not be loaded",
                "\n".join(errors[:15])
                + ("\n\u2026and more." if len(errors) > 15 else ""),
            )

        if not new_tracks:
            return

        self._save_undo()
        dw = self._disc_widgets[disc_index]
        dw.disc.tracks = new_tracks

        # Also update the global track list so undo/re-split stay consistent
        all_tracks: List[Track] = []
        for widget in self._disc_widgets:
            all_tracks.extend(widget.disc.tracks)
        self._tracks = all_tracks

        dw.refresh()
        self._update_summary()

        QMessageBox.information(
            self,
            "Reload complete",
            f"Disc {disc_index + 1} has been updated with {len(new_tracks)} "
            "resized track(s).",
        )

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
        self._natural_disc_count = 0
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
