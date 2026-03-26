# WARNING: UNTESTED CODE - This is a high-level design and code outline for a MiniDisc batch burner application. It has not been tested or run, and may contain errors or incomplete implementations. Use this as a starting point for development, but expect to need significant work to get it functional.

# MiniDisc Batch Burner

A GUI application for batch burning multiple MiniDiscs with automatic disc swapping.

## Features

- **Drag & Drop**: Drop audio files directly into the application
- **Auto-Split**: Automatically pack tracks into discs using a greedy algorithm
- **Per-Disc Config**: Customize disc size (60/74/80 min) and mode (SP/LP2/LP4) for each disc
- **Batch Burning**: Burn multiple discs with minimal user intervention
- **Progress Tracking**: Real-time progress for discs, tracks, and overall job
- **Pause/Resume**: Pause between tracks, not during uploads
- **Export Queue**: Save your disc/track listing to a text file

## Installation

### Prerequisites

1. **Python 3.8+**
2. **Node.js 18+** (for the helper script)
3. **mutagen** (for audio metadata)

### Install Python Dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Running the Application

```bash
python md_batch_burner.py
```

### Workflow

1. **Connect your MiniDisc device** (e.g., Sony MZ-N510 via USB)
2. **Drag and drop audio files** into the drop zone
3. **Configure defaults** (disc size, mode) or adjust per-disc
4. **Click "Start Burning"**
5. **Insert blank MiniDiscs** when prompted
6. **Enjoy your burned discs!** 🎉

## Architecture

- **Python + PySide6**: GUI application
- **Node.js Helper**: Device communication via `netmd-batch-helper.js`
- **mutagen**: Audio duration detection
- **JSON IPC**: Communication between Python and Node.js

## Supported Audio Formats

- MP3
- WAV
- FLAC
- OGG
- M4A
- AAC

## Disc Modes

- **SP**: Standard Play (74 min on standard disc)
- **LP2**: Long Play 2x (148 min on standard disc)
- **LP4**: Long Play 4x (296 min on standard disc)

## Disc Sizes

- 60 minutes
- 74 minutes (standard)
- 80 minutes

## Data Model

```python
@dataclass
class Track:
    path: str
    name: str
    duration_seconds: float

@dataclass
class DiscConfig:
    disc_size: int  # 60, 74, or 80
    mode: str  # SP, LP2, or LP4

@dataclass
class Disc:
    id: int
    config: DiscConfig
    tracks: List[Track]
    total_seconds: float
    status: DiscStatus
```

## Development

### Project Structure

```
md-batch-burner/
├── md_batch_burner.py       # Main PySide6 GUI
├── netmd-batch-helper.js    # Node.js helper for device communication
├── audio_converter.py       # Audio format conversion (TODO)
├── playlist_splitter.py     # Disc splitting logic (reuse)
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

### TODO

- [ ] Implement actual MiniDisc device communication (netmd-js)
- [ ] Create audio_converter.py for format conversion
- [ ] Add support for SP/LP2/LP4 specific encoding
- [ ] Implement disc title editing
- [ ] Add track reordering via drag-and-drop
- [ ] Save/load queue state
- [ ] Add batch file conversion before burning

## License

MIT License - feel free to use and modify for your own MiniDisc projects!

## Notes

The Node.js helper currently simulates device connection. To use actual MiniDisc devices, you'll need to:

1. Install `netmd-js` or similar library
2. Implement device detection and communication
3. Handle actual audio upload to MiniDisc

Happy burning! 🦞
