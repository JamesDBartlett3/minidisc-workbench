#!/usr/bin/env python3
"""
MiniDisc Batch Burner - PySide6 GUI Application
A GUI for batch burning multiple MiniDiscs with automatic disc swapping.
"""

import os
import sys
import json
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QCheckBox, QProgressBar,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QGroupBox, QFrame, QScrollArea, QDialog, QSettings
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize, QUrl
from PySide6.QtGui import (
    QColor, QPalette, QFont, QIcon, QDragEnterEvent, QDropEvent,
    QDesktopServices
)
from PySide6.QtMultimedia import QSoundEffect
from mutagen import File as MutagenFile

# Import audio conversion functions
try:
    from audio_converter import convert_for_sp, convert_for_lp2, convert_for_lp4
except ImportError:
    # Fallback stubs if audio_converter not available
    def convert_for_sp(input_path, output_path): return None
    def convert_for_lp2(input_path, output_path): return None
    def convert_for_lp4(input_path, output_path): return None


# =============================================================================
# Data Models
# =============================================================================

class DiscStatus(Enum):
    READY = "READY"
    BURNING = "BURNING"
    COMPLETE = "COMPLETE"
    WAITING_FOR_DISC = "WAITING FOR DISC"
    ERROR = "ERROR"


@dataclass
class Track:
    path: str
    name: str
    duration_seconds: float = 0.0

    @property
    def duration_display(self) -> str:
        """Return duration in MM:SS format."""
        minutes = int(self.duration_seconds // 60)
        seconds = int(self.duration_seconds % 60)
        return f"{minutes}:{seconds:02d}"


@dataclass
class DiscConfig:
    disc_size: int = 74  # 60, 74, or 80 minutes
    mode: str = "SP"     # SP, LP2, or LP4

    @property
    def capacity_seconds(self) -> int:
        """Return disc capacity in seconds based on mode."""
        mode_factors = {"SP": 1.0, "LP2": 2.0, "LP4": 4.0}
        return self.disc_size * 60 * mode_factors.get(self.mode, 1.0)


@dataclass
class Disc:
    id: int
    config: DiscConfig
    tracks: List[Track] = field(default_factory=list)
    total_seconds: float = 0.0
    status: DiscStatus = DiscStatus.READY

    @property
    def used_display(self) -> str:
        """Return used time in MM:SS format."""
        minutes = int(self.total_seconds // 60)
        seconds = int(self.total_seconds % 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def capacity_display(self) -> str:
        """Return capacity time in MM:SS format."""
        minutes = int(self.config.capacity_seconds // 60)
        seconds = int(self.config.capacity_seconds % 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def progress_percent(self) -> float:
        """Return usage percentage."""
        if self.config.capacity_seconds == 0:
            return 0.0
        return (self.total_seconds / self.config.capacity_seconds) * 100


# =============================================================================
# Notification Helper
# =============================================================================

class Notifier:
    """Handles sound and toast notifications for disc completion."""

    # Default notification sound (system beep alternative)
    DEFAULT_SOUND = None  # Will use system default

    def __init__(self, parent=None):
        self.parent = parent
        self.sound_effect = None
        self._sound_enabled = True
        self._toast_enabled = True

    @property
    def sound_enabled(self) -> bool:
        return self._sound_enabled

    @sound_enabled.setter
    def sound_enabled(self, value: bool):
        self._sound_enabled = value

    @property
    def toast_enabled(self) -> bool:
        return self._toast_enabled

    @toast_enabled.setter
    def toast_enabled(self, value: bool):
        self._toast_enabled = value

    def notify_disc_complete(self, disc_num: int, total_discs: int):
        """Send notification when a disc is complete."""
        message = f"Disc {disc_num} of {total_discs} complete! Ready to swap."

        if self._sound_enabled:
            self._play_sound()

        if self._toast_enabled:
            self._show_toast("MiniDisc Complete!", message)

    def notify_all_complete(self, total_discs: int):
        """Send notification when all discs are complete."""
        message = f"All {total_discs} MiniDiscs burned successfully!"

        if self._sound_enabled:
            self._play_sound()

        if self._toast_enabled:
            self._show_toast("Burning Complete! 🎉", message)

    def _play_sound(self):
        """Play notification sound."""
        try:
            # Try to play system notification sound
            if sys.platform == "linux":
                # Try paplay (PulseAudio) or aplay (ALSA)
                subprocess.run(
                    ["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                    capture_output=True, timeout=5
                )
            elif sys.platform == "darwin":
                # macOS - use afplay
                subprocess.run(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    capture_output=True, timeout=5
                )
            elif sys.platform == "win32":
                # Windows - use winsound (beep)
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            # Silently fail if sound can't be played
            pass

    def _show_toast(self, title: str, message: str):
        """Show desktop toast notification."""
        try:
            if sys.platform == "linux":
                # Try notify-send (libnotify)
                subprocess.run(
                    ["notify-send", "-u", "normal", "-t", "5000",
                     "-i", "media-optical", title, message],
                    capture_output=True, timeout=5
                )
            elif sys.platform == "darwin":
                # macOS - use osascript for notification
                script = f'display notification "{message}" with title "{title}"'
                subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, timeout=5
                )
            elif sys.platform == "win32":
                # Windows 10+ - use PowerShell toast
                ps_script = f'''
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
                $template = @"
                <toast>
                    <visual>
                        <binding template="ToastText02">
                            <text id="1">{title}</text>
                            <text id="2">{message}</text>
                        </binding>
                    </visual>
                </toast>
"@
                $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
                $xml.LoadXml($template)
                $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
                [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("MiniDisc Batch Burner").Show($toast)
                '''
                subprocess.run(
                    ["powershell", "-Command", ps_script],
                    capture_output=True, timeout=10
                )
        except Exception:
            # Silently fail if toast can't be shown
            pass


# =============================================================================
# Node.js Helper Communication
# =============================================================================

def call_helper(command_dict: dict) -> dict:
    """Call the Node.js helper via subprocess."""
    script_dir = Path(__file__).parent
    helper_path = script_dir / "netmd-batch-helper.js"

    if not helper_path.exists():
        raise FileNotFoundError(f"Helper script not found: {helper_path}")

    try:
        proc = subprocess.Popen(
            ['node', str(helper_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(script_dir)
        )
        stdout, stderr = proc.communicate(json.dumps(command_dict) + '\n')

        if proc.returncode != 0:
            raise RuntimeError(f"Helper error: {stderr}")

        return json.loads(stdout)
    except FileNotFoundError:
        raise RuntimeError("Node.js is not installed or not in PATH")
    except json.JSONDecodeError:
        raise RuntimeError(f"Invalid JSON response from helper: {stdout}")


# =============================================================================
# Worker Thread for Burning
# =============================================================================

class BurnWorker(QThread):
    """Background thread for burning process."""

    # Signals
    progress_updated = Signal(int, int, int, float)  # disc_idx, track_idx, total_tracks, percent
    status_updated = Signal(str)  # status message
    disc_complete = Signal(int)  # disc index
    burning_complete = Signal()
    error_occurred = Signal(str)  # error message
    paused = Signal()

    def __init__(self, discs: List[Disc], default_config: DiscConfig):
        super().__init__()
        self.discs = discs
        self.default_config = default_config
        self._running = False
        self._paused = False
        self._pause_event = None

    def run(self):
        """Main burning loop."""
        self._running = True

        try:
            # Check device connection
            device_info = call_helper({"action": "get_device"})
            if not device_info.get("connected", False):
                self.error_occurred.emit("No MiniDisc device connected")
                return

            for disc_idx, disc in enumerate(self.discs):
                if not self._running:
                    break

                disc.status = DiscStatus.BURNING
                self.status_updated.emit(f"Starting Disc {disc_idx + 1} of {len(self.discs)}...")

                # Burn each track
                for track_idx, track in enumerate(disc.tracks):
                    if not self._running:
                        break

                    # Check for pause
                    while self._paused:
                        self.paused.emit()
                        self.msleep(100)
                        if not self._running:
                            return

                    # Update progress
                    percent = (track_idx / len(disc.tracks)) * 100
                    self.progress_updated.emit(disc_idx, track_idx, len(disc.tracks), percent)
                    self.status_updated.emit(
                        f"Processing '{track.name}' ({track_idx + 1}/{len(disc.tracks)})..."
                    )

                    # Convert audio (placeholder - would call audio_converter)
                    self.status_updated.emit(f"Converting '{track.name}' to {disc.config.mode} format...")
                    try:
                        stem = Path(track.path).stem
                        if disc.config.mode == "SP":
                            converted_path = os.path.join(temp_dir, stem + '.raw')
                            success = convert_for_sp(track.path, converted_path)
                        elif disc.config.mode == "LP2":
                            converted_path = os.path.join(temp_dir, stem + '.oma')
                            success = convert_for_lp2(track.path, converted_path)
                        else:  # LP4
                            converted_path = os.path.join(temp_dir, stem + '.oma')
                            success = convert_for_lp4(track.path, converted_path)
                        
                        upload_path = converted_path if success else track.path
                    except Exception as e:
                        self.error_occurred.emit(f"Failed to convert '{track.name}': {str(e)}")
                        disc.status = DiscStatus.ERROR
                        return

                    # Upload to device
                    self.status_updated.emit(f"Uploading '{track.name}' to MiniDisc...")
                    try:
                        call_helper({
                            "action": "upload_track",
                            "path": track.path,
                            "mode": disc.config.mode
                        })
                        self.msleep(300)  # Simulate upload time
                    except Exception as e:
                        self.error_occurred.emit(f"Failed to upload '{track.name}': {str(e)}")
                        disc.status = DiscStatus.ERROR
                        return

                # Disc complete
                disc.status = DiscStatus.COMPLETE
                self.disc_complete.emit(disc_idx)
                self.progress_updated.emit(disc_idx, len(disc.tracks), len(disc.tracks), 100.0)

                # Check if more discs to process
                if disc_idx < len(self.discs) - 1:
                    self.status_updated.emit("Disc complete! Waiting for disc swap...")
                    disc.status = DiscStatus.WAITING_FOR_DISC
                    break  # Wait for user to continue

            if self._running:
                self.burning_complete.emit()

        except Exception as e:
            self.error_occurred.emit(f"Burning failed: {str(e)}")
        finally:
            self._running = False

    def pause(self):
        """Pause burning."""
        self._paused = True

    def resume(self):
        """Resume burning."""
        self._paused = False

    def stop(self):
        """Stop burning."""
        self._running = False
        self._paused = False
        self.wait()


# =============================================================================
# Custom Widgets
# =============================================================================

class DropZone(QFrame):
    """Drag and drop zone for audio files."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(100)
        self.setFrameStyle(QFrame.Box | QFrame.Raised)
        self.setLineWidth(2)

        layout = QVBoxLayout(self)
        self.label = QLabel("🎵 Drop music files here\n(or click to browse)")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: #888; font-size: 14px;")
        layout.addWidget(self.label)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("background-color: #2a2a2a; border: 2px dashed #4a9eff;")

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        files = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                files.append(url.toLocalFile())
        if files:
            self.files_dropped.emit(files)

    def mousePressEvent(self, event):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Audio Files",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.ogg *.m4a *.aac);;All Files (*)"
        )
        if files:
            self.files_dropped.emit(files)


class DiscWidget(QGroupBox):
    """Widget displaying a single disc with its tracks."""

    config_changed = Signal(int, DiscConfig)

    def __init__(self, disc: Disc, parent=None):
        super().__init__(f"Disc {disc.id}", parent)
        self.disc = disc

        layout = QVBoxLayout(self)

        # Header with capacity and status
        header_layout = QHBoxLayout()

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignRight)
        self.status_label.setStyleSheet("font-weight: bold;")
        header_layout.addWidget(self.status_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #444;
                border-radius: 3px;
                background-color: #222;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #4a9eff;
                border-radius: 2px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # Capacity label
        self.capacity_label = QLabel()
        layout.addWidget(self.capacity_label)

        # Tracks list
        self.tracks_widget = QListWidget()
        self.tracks_widget.setMaximumHeight(150)
        self.tracks_widget.setStyleSheet("""
            QListWidget {
                background-color: #1a1a1a;
                border: 1px solid #333;
                border-radius: 3px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 4px;
                border-bottom: 1px solid #2a2a2a;
            }
        """)
        layout.addWidget(self.tracks_widget)

        # Config controls
        config_layout = QHBoxLayout()

        # Disc size
        config_layout.addWidget(QLabel("Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItems(["60 min", "74 min", "80 min"])
        self.size_combo.setCurrentIndex([60, 74, 80].index(disc.config.disc_size))
        self.size_combo.currentIndexChanged.connect(self._update_config)
        config_layout.addWidget(self.size_combo)

        config_layout.addSpacing(20)

        # Mode
        config_layout.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["SP", "LP2", "LP4"])
        self.mode_combo.setCurrentText(disc.config.mode)
        self.mode_combo.currentIndexChanged.connect(self._update_config)
        config_layout.addWidget(self.mode_combo)

        config_layout.addStretch()
        layout.addLayout(config_layout)

        self._update_display()

    def _update_config(self):
        """Update disc configuration."""
        self.disc.config.disc_size = [60, 74, 80][self.size_combo.currentIndex()]
        self.disc.config.mode = self.mode_combo.currentText()
        self.config_changed.emit(self.disc.id, self.disc.config)
        self._update_display()

    def _update_display(self):
        """Update display elements."""
        # Status
        status_colors = {
            DiscStatus.READY: "#4eff4e",
            DiscStatus.BURNING: "#ffcc00",
            DiscStatus.COMPLETE: "#4a9eff",
            DiscStatus.WAITING_FOR_DISC: "#ffaa00",
            DiscStatus.ERROR: "#ff4a4a"
        }
        color = status_colors.get(self.disc.status, "#888")
        self.status_label.setText(f"● {self.disc.status.value}")
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")

        # Progress
        self.progress_bar.setValue(int(self.disc.progress_percent))

        # Capacity
        self.capacity_label.setText(
            f"{self.disc.used_display} / {self.disc.capacity_display} ({int(self.disc.progress_percent)}%)"
        )

        # Tracks
        self.tracks_widget.clear()
        for i, track in enumerate(self.disc.tracks):
            item_text = f"{i + 1}. {track.name} ... {track.duration_display}"
            self.tracks_widget.addItem(item_text)

    def update_status(self, status: DiscStatus):
        """Update disc status."""
        self.disc.status = status
        self._update_display()

    def update_progress(self, percent: float):
        """Update progress bar."""
        self.progress_bar.setValue(int(percent))


# =============================================================================
# Disc Swap Dialog
# =============================================================================

class DiscSwapDialog(QDialog):
    """Dialog shown when disc swap is needed."""

    def __init__(self, disc_num: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Disc Swap Required")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        message = QLabel(
            f"Disc {disc_num} is complete!\n\n"
            "Please eject the finished disc and insert a blank MiniDisc "
            f"for Disc {disc_num + 1}."
        )
        message.setAlignment(Qt.AlignCenter)
        message.setStyleSheet("font-size: 14px; padding: 20px;")
        layout.addWidget(message)

        button_layout = QHBoxLayout()
        button_layout.addStretch()

        continue_btn = QPushButton("Continue")
        continue_btn.clicked.connect(self.accept)
        button_layout.addWidget(continue_btn)

        layout.addLayout(button_layout)


# =============================================================================
# Main Application
# =============================================================================

class MiniDiscBatchBurner(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()

        # Data
        self.tracks: List[Track] = []
        self.discs: List[Disc] = []
        self.default_config = DiscConfig()
        self.auto_split = True
        self.burn_worker: Optional[BurnWorker] = None

        # Settings
        self.settings = QSettings("MiniDiscBatchBurner", "MiniDiscBatchBurner")

        # Notifications
        self.notifier = Notifier(self)
        self.notifier.sound_enabled = self.settings.value("notifications/sound", True, type=bool)
        self.notifier.toast_enabled = self.settings.value("notifications/toast", True, type=bool)

        # Setup UI
        self._setup_dark_theme()
        self._setup_ui()
        self._start_device_polling()

    def _setup_dark_theme(self):
        """Set up dark theme matching MiniDisc aesthetic."""
        palette = QPalette()

        # Charcoal background
        palette.setColor(QPalette.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.WindowText, QColor(220, 220, 220))

        # Widgets
        palette.setColor(QPalette.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.AlternateBase, QColor(35, 35, 35))
        palette.setColor(QPalette.ToolTipBase, QColor(220, 220, 220))
        palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
        palette.setColor(QPalette.Text, QColor(220, 220, 220))
        palette.setColor(QPalette.Button, QColor(50, 50, 50))
        palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
        palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
        palette.setColor(QPalette.Link, QColor(74, 158, 255))
        palette.setColor(QPalette.Highlight, QColor(74, 158, 255))
        palette.setColor(QPalette.HighlightedText, QColor(0, 0, 0))

        QApplication.setPalette(palette)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QLabel {
                color: #ddd;
            }
            QGroupBox {
                color: #ccc;
                border: 1px solid #444;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QPushButton {
                background-color: #333;
                color: #eee;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #444;
                border: 1px solid #4a9eff;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QPushButton:disabled {
                color: #666;
                background-color: #222;
            }
            QComboBox {
                background-color: #333;
                color: #eee;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px 8px;
            }
            QComboBox:hover {
                border: 1px solid #4a9eff;
            }
            QComboBox::drop-down {
                border: none;
                background-color: #333;
            }
            QComboBox QAbstractItemView {
                background-color: #333;
                color: #eee;
                border: 1px solid #555;
                selection-background-color: #4a9eff;
            }
            QCheckBox {
                color: #ddd;
            }
            QProgressBar {
                border: 1px solid #444;
                border-radius: 3px;
                background-color: #222;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #4a9eff;
                border-radius: 2px;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
        """)

    def _setup_ui(self):
        """Set up the user interface."""
        self.setWindowTitle("MiniDisc Batch Burner")
        self.setMinimumSize(900, 700)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # Device Status
        status_group = QGroupBox("Device Status")
        status_layout = QHBoxLayout(status_group)

        self.device_status_label = QLabel("● Checking...")
        self.device_status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
        status_layout.addWidget(self.device_status_label)

        status_layout.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._check_device)
        status_layout.addWidget(refresh_btn)

        main_layout.addWidget(status_group)

        # Drop Zone
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._handle_dropped_files)
        main_layout.addWidget(self.drop_zone)

        # Default Configuration
        config_group = QGroupBox("Default Settings")
        config_layout = QHBoxLayout(config_group)

        config_layout.addWidget(QLabel("Default:"))

        # Disc size
        self.default_size_combo = QComboBox()
        self.default_size_combo.addItems(["60 min", "74 min", "80 min"])
        self.default_size_combo.setCurrentIndex([60, 74, 80].index(self.default_config.disc_size))
        self.default_size_combo.currentIndexChanged.connect(self._update_default_config)
        config_layout.addWidget(self.default_size_combo)

        # Mode
        self.default_mode_combo = QComboBox()
        self.default_mode_combo.addItems(["SP", "LP2", "LP4"])
        self.default_mode_combo.setCurrentText(self.default_config.mode)
        self.default_mode_combo.currentIndexChanged.connect(self._update_default_config)
        config_layout.addWidget(self.default_mode_combo)

        config_layout.addSpacing(20)

        # Auto-split checkbox
        self.auto_split_check = QCheckBox("Auto-Split into Discs")
        self.auto_split_check.setChecked(self.auto_split)
        self.auto_split_check.toggled.connect(self._toggle_auto_split)
        config_layout.addWidget(self.auto_split_check)

        config_layout.addStretch()

        # Clear button
        clear_btn = QPushButton("Clear Queue")
        clear_btn.clicked.connect(self._clear_queue)
        config_layout.addWidget(clear_btn)

        main_layout.addWidget(config_group)

        # Notifications Settings
        notif_group = QGroupBox("Notifications")
        notif_layout = QHBoxLayout(notif_group)

        # Sound notification checkbox
        self.sound_check = QCheckBox("🔊 Sound on disc complete")
        self.sound_check.setChecked(self.notifier.sound_enabled)
        self.sound_check.toggled.connect(self._toggle_sound)
        notif_layout.addWidget(self.sound_check)

        notif_layout.addSpacing(20)

        # Toast notification checkbox
        self.toast_check = QCheckBox("🔔 Desktop notification")
        self.toast_check.setChecked(self.notifier.toast_enabled)
        self.toast_check.toggled.connect(self._toggle_toast)
        notif_layout.addWidget(self.toast_check)

        notif_layout.addStretch()

        main_layout.addWidget(notif_group)

        # Discs List
        discs_label = QLabel("Disc Queue")
        discs_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        main_layout.addWidget(discs_label)

        self.discs_scroll = QScrollArea()
        self.discs_scroll.setWidgetResizable(True)
        self.discs_scroll.setMinimumHeight(300)

        self.discs_container = QWidget()
        self.discs_layout = QVBoxLayout(self.discs_container)
        self.discs_layout.setAlignment(Qt.AlignTop)
        self.discs_layout.setSpacing(10)

        self.discs_scroll.setWidget(self.discs_container)
        main_layout.addWidget(self.discs_scroll)

        # Controls
        controls_layout = QHBoxLayout()

        self.start_btn = QPushButton("▶ Start Burning")
        self.start_btn.setMinimumWidth(150)
        self.start_btn.clicked.connect(self._start_burning)
        controls_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.setMinimumWidth(100)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._pause_burning)
        controls_layout.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.setMinimumWidth(100)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_burning)
        controls_layout.addWidget(self.stop_btn)

        controls_layout.addStretch()

        export_btn = QPushButton("📋 Export Queue")
        export_btn.clicked.connect(self._export_queue)
        controls_layout.addWidget(export_btn)

        main_layout.addLayout(controls_layout)

        # Progress Bar
        main_layout.addSpacing(10)
        self.main_progress = QProgressBar()
        self.main_progress.setMaximum(100)
        self.main_progress.setTextVisible(True)
        main_layout.addWidget(self.main_progress)

        # Status Label
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888; font-style: italic;")
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)

    def _start_device_polling(self):
        """Start polling for device status."""
        self.device_timer = QTimer()
        self.device_timer.timeout.connect(self._check_device)
        self.device_timer.start(5000)  # Poll every 5 seconds
        self._check_device()

    def _check_device(self):
        """Check device connection status."""
        try:
            result = call_helper({"action": "get_device"})
            if result.get("connected", False):
                device_name = result.get("name", "Unknown Device")
                self.device_status_label.setText(f"● Connected - {device_name}")
                self.device_status_label.setStyleSheet("color: #4eff4e; font-weight: bold;")
            else:
                self.device_status_label.setText("● Not Connected")
                self.device_status_label.setStyleSheet("color: #ff4a4a; font-weight: bold;")
        except Exception:
            self.device_status_label.setText("● Helper Error")
            self.device_status_label.setStyleSheet("color: #ff4a4a; font-weight: bold;")

    def _handle_dropped_files(self, files: List[str]):
        """Handle dropped audio files."""
        for file_path in files:
            try:
                # Get file info
                path = Path(file_path)
                name = path.stem

                # Get duration using mutagen
                audio = MutagenFile(file_path)
                if audio and audio.info:
                    duration = audio.info.length
                else:
                    duration = 0

                track = Track(path=str(file_path), name=name, duration_seconds=duration)
                self.tracks.append(track)

            except Exception as e:
                QMessageBox.warning(
                    self,
                    "File Error",
                    f"Could not load '{file_path}': {str(e)}"
                )

        self._update_queue()

    def _update_default_config(self):
        """Update default configuration."""
        self.default_config.disc_size = [60, 74, 80][self.default_size_combo.currentIndex()]
        self.default_config.mode = self.default_mode_combo.currentText()

        if self.auto_split:
            self._split_into_discs()
        else:
            # Update all discs with new defaults
            for disc in self.discs:
                disc.config = DiscConfig(self.default_config.disc_size, self.default_config.mode)
            self._refresh_disc_widgets()

    def _toggle_auto_split(self, checked: bool):
        """Toggle auto-split mode."""
        self.auto_split = checked
        if checked:
            self._split_into_discs()
        else:
            # Keep current discs but allow manual editing
            pass

    def _toggle_sound(self, checked: bool):
        """Toggle sound notifications."""
        self.notifier.sound_enabled = checked
        self.settings.setValue("notifications/sound", checked)

    def _toggle_toast(self, checked: bool):
        """Toggle toast notifications."""
        self.notifier.toast_enabled = checked
        self.settings.setValue("notifications/toast", checked)

    def _split_into_discs(self):
        """Split tracks into discs using greedy algorithm."""
        if not self.tracks:
            self.discs = []
            self._refresh_disc_widgets()
            return

        self.discs = []
        current_disc = Disc(id=1, config=DiscConfig(self.default_config.disc_size, self.default_config.mode))

        for track in self.tracks:
            # Check if track fits
            if current_disc.total_seconds + track.duration_seconds > current_disc.config.capacity_seconds:
                # Start new disc
                self.discs.append(current_disc)
                current_disc = Disc(
                    id=len(self.discs) + 1,
                    config=DiscConfig(self.default_config.disc_size, self.default_config.mode)
                )

            current_disc.tracks.append(track)
            current_disc.total_seconds += track.duration_seconds

        if current_disc.tracks:
            self.discs.append(current_disc)

        self._refresh_disc_widgets()

    def _refresh_disc_widgets(self):
        """Refresh disc widgets in the UI."""
        # Clear existing widgets
        for i in reversed(range(self.discs_layout.count())):
            child = self.discs_layout.itemAt(i).widget()
            if child:
                child.deleteLater()

        # Add new widgets
        for disc in self.discs:
            widget = DiscWidget(disc)
            widget.config_changed.connect(self._on_disc_config_changed)
            self.discs_layout.addWidget(widget)

    def _on_disc_config_changed(self, disc_id: int, config: DiscConfig):
        """Handle disc configuration change."""
        for disc in self.discs:
            if disc.id == disc_id:
                disc.config = config
                break

    def _update_queue(self):
        """Update queue display."""
        if self.auto_split:
            self._split_into_discs()
        else:
            self._refresh_disc_widgets()

        self.status_label.setText(f"Added {len(self.tracks)} track(s) to queue")

    def _clear_queue(self):
        """Clear the current queue."""
        self.tracks = []
        self.discs = []
        self._refresh_disc_widgets()
        self.status_label.setText("Queue cleared")

    def _start_burning(self):
        """Start the burning process."""
        if not self.discs:
            QMessageBox.warning(self, "No Discs", "Please add audio files to create discs.")
            return

        if self.burn_worker and self.burn_worker.isRunning():
            return

        self.burn_worker = BurnWorker(self.discs, self.default_config)
        self.burn_worker.progress_updated.connect(self._on_progress_updated)
        self.burn_worker.status_updated.connect(self._on_status_updated)
        self.burn_worker.disc_complete.connect(self._on_disc_complete)
        self.burn_worker.burning_complete.connect(self._on_burning_complete)
        self.burn_worker.error_occurred.connect(self._on_error)
        self.burn_worker.paused.connect(self._on_paused)

        self.burn_worker.start()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)

    def _pause_burning(self):
        """Pause/resume burning."""
        if not self.burn_worker:
            return

        if self.burn_worker._paused:
            self.burn_worker.resume()
            self.pause_btn.setText("⏸ Pause")
            self.status_label.setText("Resuming...")
        else:
            self.burn_worker.pause()
            self.pause_btn.setText("▶ Resume")

    def _stop_burning(self):
        """Stop burning."""
        if self.burn_worker and self.burn_worker.isRunning():
            self.burn_worker.stop()

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("⏸ Pause")
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Stopped")

    def _on_progress_updated(self, disc_idx: int, track_idx: int, total_tracks: int, percent: float):
        """Handle progress update."""
        # Update disc widget
        if disc_idx < len(self.discs):
            widget = self.discs_layout.itemAt(disc_idx).widget()
            if widget:
                widget.update_progress(percent)

        # Calculate overall progress
        total_discs = len(self.discs)
        if total_discs > 0:
            overall_percent = ((disc_idx * 100) + percent) / total_discs
            self.main_progress.setValue(int(overall_percent))

    def _on_status_updated(self, message: str):
        """Handle status update."""
        self.status_label.setText(message)

    def _on_disc_complete(self, disc_idx: int):
        """Handle disc completion."""
        if disc_idx < len(self.discs):
            widget = self.discs_layout.itemAt(disc_idx).widget()
            if widget:
                widget.update_status(DiscStatus.COMPLETE)

        # Send notification
        self.notifier.notify_disc_complete(disc_idx + 1, len(self.discs))

        # Check if more discs to process
        if disc_idx < len(self.discs) - 1:
            # Show disc swap dialog
            self.burn_worker.pause()

            dialog = DiscSwapDialog(disc_idx + 1, self)
            if dialog.exec() == QDialog.Accepted:
                self.burn_worker.resume()

                # Update next disc status to READY
                if disc_idx + 1 < len(self.discs):
                    widget = self.discs_layout.itemAt(disc_idx + 1).widget()
                    if widget:
                        widget.update_status(DiscStatus.READY)

    def _on_burning_complete(self):
        """Handle burning completion."""
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.main_progress.setValue(100)
        self.status_label.setText("All discs complete! 🎉")

        # Send notification
        self.notifier.notify_all_complete(len(self.discs))

        QMessageBox.information(
            self,
            "Burning Complete",
            "All MiniDiscs have been burned successfully!"
        )

    def _on_error(self, message: str):
        """Handle error."""
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.status_label.setText(f"Error: {message}")

        QMessageBox.critical(
            self,
            "Burning Error",
            message
        )

    def _on_paused(self):
        """Handle paused state."""
        self.status_label.setText("Paused (between tracks)")

    def _export_queue(self):
        """Export queue to text file."""
        if not self.discs:
            QMessageBox.warning(self, "Empty Queue", "No discs to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Queue",
            "minidisc_queue.txt",
            "Text Files (*.txt);;All Files (*)"
        )

        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("MiniDisc Batch Burner - Queue Export\n")
                f.write("=" * 50 + "\n\n")

                for disc in self.discs:
                    f.write(f"Disc {disc.id} - {disc.config.disc_size} min {disc.config.mode}\n")
                    f.write(f"Capacity: {disc.used_display} / {disc.capacity_display}\n")
                    f.write("-" * 40 + "\n")

                    for i, track in enumerate(disc.tracks):
                        f.write(f"  {i + 1}. {track.name}\n")
                        f.write(f"      Duration: {track.duration_display}\n")
                        f.write(f"      Path: {track.path}\n")

                    f.write("\n")

            QMessageBox.information(
                self,
                "Export Complete",
                f"Queue exported to:\n{path}"
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"Could not export queue:\n{str(e)}"
            )

    def closeEvent(self, event):
        """Handle window close event."""
        if self.burn_worker and self.burn_worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Confirm Exit",
                "Burning is in progress. Are you sure you want to exit?",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                self.burn_worker.stop()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MiniDisc Batch Burner")

    window = MiniDiscBatchBurner()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
