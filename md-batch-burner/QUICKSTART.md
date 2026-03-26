# Quick Start Guide

## Prerequisites

Before running the MiniDisc Batch Burner, ensure you have:

1. **Python 3.8+** installed
2. **Node.js 16+** installed
3. **FFmpeg** (for audio conversion) - optional, but recommended
4. A MiniDisc recorder connected via USB (e.g., Sony MZ-N510, MZ-NH600, etc.)

## Installation

### 1. Clone/Download the Project

```bash
cd /home/james/.openclaw/workspace/innovation/projects/md-batch-burner
```

### 2. Install Python Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `PySide6` - GUI framework
- `mutagen` - Audio metadata extraction

### 3. Install Node.js Dependencies

```bash
npm install
```

This installs:
- `netmd-js` - MiniDisc device communication library

### 4. (Optional) Install FFmpeg

For audio conversion:

**Ubuntu/Debian:**
```bash
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html)

## Verification

Run the verification script:

```bash
python verify.py
```

You should see:
```
All checks passed! ✅
```

## Running the Application

Start the GUI:

```bash
python md_batch_burner.py
```

## Basic Workflow

1. **Connect your MiniDisc** to the computer via USB
2. **Wait for device connection** - The GUI will show "● Connected - [Device Name]"
3. **Drag and drop audio files** into the drop zone
   - Supports: MP3, WAV, FLAC, OGG, M4A, AAC
4. **Configure settings** (optional):
   - Default disc size: 60/74/80 minutes
   - Default mode: SP/LP2/LP4
   - Auto-split into discs (recommended)
5. **Review discs** - The app will automatically pack tracks into discs
   - Adjust per-disc settings if needed
6. **Click "Start Burning"**
7. **Insert blank MiniDisc** when prompted
8. **Wait for burning to complete** 🎉

## Disc Modes Explained

| Mode | Quality | Capacity (74 min disc) | Best For |
|------|---------|------------------------|----------|
| **SP** | High | 74 min | Critical listening, archives |
| **LP2** | Good | 148 min | Most music, casual listening |
| **LP4** | Standard | 296 min | Spoken word, background music |

## Troubleshooting

### Device Not Connecting

1. Check USB connection
2. Try a different USB port
3. Restart the MiniDisc recorder
4. Click "Refresh" in the GUI

### Audio Conversion Errors

1. Install FFmpeg (see above)
2. Check file format is supported
3. Ensure files aren't corrupted

### Node.js Helper Errors

1. Run `npm install` in the project directory
2. Ensure Node.js version is 16+
3. Check console for specific error messages

## Advanced Usage

### Manual Disc Configuration

Uncheck "Auto-Split into Discs" to manually configure each disc:
1. Drop files into the queue
2. Adjust size/mode for each