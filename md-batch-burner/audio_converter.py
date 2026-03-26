#!/usr/bin/env python3
"""
Audio Converter for MiniDisc Batch Burner

Converts audio files to formats suitable for NetMD upload:
- SP mode: Raw PCM (16-bit, big-endian, 44.1kHz, stereo)
- LP2 mode: ATRAC3 128kbps
- LP4 mode: ATRAC3 64kbps

For LP2/LP4 modes, supports two encoders:
1. psp_at3tool.exe (Sony's proprietary encoder) - Higher quality, Windows-only
2. atracdenc (open source) - Lower quality, cross-platform

The encoder can be configured via:
- Environment variable: PSP_AT3TOOL_PATH
- Config file: ~/.md-batch-burner/config.json
- Runtime parameter in check_dependencies() and convert functions
"""

import subprocess
import os
import sys
import logging
import tempfile
import shutil
import json
from pathlib import Path
from typing import Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Supported input formats
SUPPORTED_INPUT_FORMATS = {'.mp3', '.flac', '.wav', '.m4a', '.ogg', '.opus', '.wma', '.aac', '.aiff'}

# Default config location
CONFIG_PATH = Path.home() / '.md-batch-burner' / 'config.json'


def get_psp_at3tool_path(custom_path: Optional[str] = None) -> Optional[str]:
    """
    Get the path to psp_at3tool.exe from various sources.

    Priority:
    1. Custom path passed directly
    2. Environment variable PSP_AT3TOOL_PATH
    3. Config file ~/.md-batch-burner/config.json

    Returns:
        Path to psp_at3tool.exe if configured, None otherwise
    """
    # 1. Direct parameter
    if custom_path and os.path.isfile(custom_path):
        return custom_path

    # 2. Environment variable
    env_path = os.environ.get('PSP_AT3TOOL_PATH')
    if env_path and os.path.isfile(env_path):
        return env_path

    # 3. Config file
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                config = json.load(f)
            config_path = config.get('psp_at3tool_path')
            if config_path and os.path.isfile(config_path):
                return config_path
        except (json.JSONDecodeError, IOError) as e:
            logger.debug(f"Could not read config file: {e}")

    return None


def check_dependencies(psp_at3tool_path: Optional[str] = None) -> dict:
    """
    Check if required dependencies are available.

    Args:
        psp_at3tool_path: Optional custom path to psp_at3tool.exe

    Returns:
        dict: Dictionary with keys 'ffmpeg', 'ffprobe', 'atracdenc', 'psp_at3tool' and boolean values
    """
    dependencies = {
        'ffmpeg': False,
        'ffprobe': False,
        'atracdenc': False,
        'psp_at3tool': False
    }

    # Check standard tools
    for tool in ['ffmpeg', 'ffprobe', 'atracdenc']:
        try:
            result = subprocess.run(
                [tool, '-version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            dependencies[tool] = result.returncode == 0
            if dependencies[tool]:
                logger.debug(f"✓ {tool} found")
            else:
                logger.warning(f"✗ {tool} not found or failed to run")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            dependencies[tool] = False
            logger.warning(f"✗ {tool} not available: {e}")

    # Check psp_at3tool (optional, higher quality encoder)
    at3tool = get_psp_at3tool_path(psp_at3tool_path)
    if at3tool:
        try:
            # psp_at3tool doesn't have a version flag, just check if it exists and is executable
            result = subprocess.run(
                [at3tool],
                capture_output=True,
                text=True,
                timeout=5
            )
            # It may return error without args, but that's fine - it exists
            dependencies['psp_at3tool'] = True
            dependencies['psp_at3tool_path'] = at3tool
            logger.debug(f"✓ psp_at3tool found at: {at3tool}")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            dependencies['psp_at3tool'] = False
            logger.debug(f"✗ psp_at3tool not available: {e}")
    else:
        logger.debug("✗ psp_at3tool not configured (set PSP_AT3TOOL_PATH env var or add to config)")

    return dependencies


def get_audio_duration(path: str) -> float:
    """
    Get the duration of an audio file in seconds.

    Args:
        path: Path to the audio file

    Returns:
        float: Duration in seconds, or 0.0 if unable to determine

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If the file format is not supported
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Audio file not found: {path}")

    ext = Path(path).suffix.lower()
    if ext not in SUPPORTED_INPUT_FORMATS:
        raise ValueError(f"Unsupported input format: {ext}")

    try:
        result = subprocess.run(
            [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                path
            ],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            logger.error(f"ffprobe failed: {result.stderr}")
            return 0.0

        duration_str = result.stdout.strip()
        duration = float(duration_str)
        logger.debug(f"Duration of {path}: {duration:.2f} seconds")
        return duration

    except subprocess.TimeoutExpired:
        logger.error("ffprobe timed out")
        return 0.0
    except ValueError as e:
        logger.error(f"Failed to parse duration: {e}")
        return 0.0
    except Exception as e:
        logger.error(f"Unexpected error getting duration: {e}")
        return 0.0


def convert_for_sp(input_path: str, output_path: str) -> bool:
    """
    Convert audio to raw PCM format for SP mode upload.

    Output format: Raw PCM, big-endian, 16-bit, 44100Hz, stereo

    Args:
        input_path: Path to input audio file
        output_path: Path to output raw PCM file

    Returns:
        bool: True if conversion successful, False otherwise
    """
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return False

    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_INPUT_FORMATS:
        logger.error(f"Unsupported input format: {ext}")
        return False

    try:
        # Create output directory if needed
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

        logger.info(f"Converting {input_path} to SP format (raw PCM)...")

        result = subprocess.run(
            [
                'ffmpeg',
                '-i', input_path,
                '-f', 's16be',          # Raw PCM, signed 16-bit, big-endian
                '-ar', '44100',          # 44.1kHz sample rate
                '-ac', '2',              # Stereo
                '-y',                    # Overwrite output file if exists
                output_path
            ],
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result.returncode != 0:
            logger.error(f"ffmpeg conversion failed: {result.stderr}")
            return False

        logger.info(f"✓ SP conversion complete: {output_path}")
        return True

    except subprocess.TimeoutExpired:
        logger.error("Conversion timed out after 5 minutes")
        return False
    except Exception as e:
        logger.error(f"Error during SP conversion: {e}")
        return False


def _convert_to_wav_intermediate(input_path: str, wav_path: str) -> bool:
    """
    Convert input audio to 44100Hz 16-bit stereo WAV (intermediate step for LP2/LP4).

    Args:
        input_path: Path to input audio file
        wav_path: Path to output WAV file

    Returns:
        bool: True if conversion successful, False otherwise
    """
    try:
        result = subprocess.run(
            [
                'ffmpeg',
                '-i', input_path,
                '-ar', '44100',          # 44.1kHz sample rate
                '-ac', '2',              # Stereo
                '-acodec', 'pcm_s16le',  # 16-bit PCM, little-endian
                '-y',                    # Overwrite output file if exists
                wav_path
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            logger.error(f"WAV conversion failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("WAV conversion timed out")
        return False
    except Exception as e:
        logger.error(f"Error during WAV conversion: {e}")
        return False


def _encode_atrac3(wav_path: str, oma_path: str, bitrate: int) -> bool:
    """
    Encode WAV to ATRAC3 using atracdenc.

    Args:
        wav_path: Path to input WAV file
        oma_path: Path to output OMA file
        bitrate: Bitrate in kbps (128 for LP2, 64 for LP4)

    Returns:
        bool: True if encoding successful, False otherwise
    """
    try:
        result = subprocess.run(
            [
                'atracdenc',
                '-e', 'atrac3',
                '-i', wav_path,
                '-o', oma_path,
                '--bitrate', str(bitrate)
            ],
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout for encoding
        )

        if result.returncode != 0:
            logger.error(f"atracdenc encoding failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("ATRAC3 encoding timed out")
        return False
    except Exception as e:
        logger.error(f"Error during ATRAC3 encoding: {e}")
        return False


def _encode_atrac3_psp(at3tool_path: str, wav_path: str, output_path: str, bitrate: int) -> bool:
    """
    Encode WAV to ATRAC3 using Sony's psp_at3tool.exe (higher quality).

    Args:
        at3tool_path: Path to psp_at3tool.exe
        wav_path: Path to input WAV file
        output_path: Path to output raw ATRAC3 file
        bitrate: Bitrate in kbps (128 for LP2, 64 for LP4)

    Returns:
        bool: True if encoding successful, False otherwise
    """
    try:
        # psp_at3tool expects bitrate in bits per second
        bitrate_bps = bitrate * 1000

        # Create temp .at3 file first
        temp_at3 = output_path + '.temp.at3'

        result = subprocess.run(
            [
                at3tool_path,
                '-e',           # Encode mode
                wav_path,       # Input WAV
                temp_at3,       # Output AT3
                '-br', str(bitrate_bps)  # Bitrate in bps
            ],
            capture_output=True,
            text=True,
            timeout=600
        )

        if result.returncode != 0:
            logger.error(f"psp_at3tool encoding failed: {result.stderr}")
            return False

        # Strip RIFF/WAV header from .at3 file (first 96 bytes, similar to OMA)
        # psp_at3tool outputs a WAV-like container that needs stripping
        result = subprocess.run(
            [
                'dd',
                f'if={temp_at3}',
                f'of={output_path}',
                'bs=96',
                'skip=1',
                'status=none'
            ],
            capture_output=True,
            text=True,
            timeout=60
        )

        # Clean up temp file
        try:
            os.remove(temp_at3)
        except:
            pass

        if result.returncode != 0:
            logger.error(f"dd header strip failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("psp_at3tool encoding timed out")
        return False
    except Exception as e:
        logger.error(f"Error during psp_at3tool encoding: {e}")
        return False


def _strip_oma_header(oma_path: str, output_path: str) -> bool:
    """
    Strip OMA header from ATRAC3 file using dd.

    OMA files have a 96-byte header that needs to be removed.

    Args:
        oma_path: Path to input OMA file
        output_path: Path to output raw ATRAC3 file

    Returns:
        bool: True if stripping successful, False otherwise
    """
    try:
        result = subprocess.run(
            [
                'dd',
                f'if={oma_path}',
                f'of={output_path}',
                'bs=96',
                'skip=1',
                'status=none'
            ],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            logger.error(f"dd command failed: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error("Header stripping timed out")
        return False
    except Exception as e:
        logger.error(f"Error during header stripping: {e}")
        return False


def _convert_lp_common(input_path: str, output_path: str, bitrate: int, mode_name: str,
                        psp_at3tool_path: Optional[str] = None) -> bool:
    """
    Common conversion logic for LP2 and LP4 modes.

    Uses psp_at3tool.exe if available (higher quality), otherwise falls back to atracdenc.

    Args:
        input_path: Path to input audio file
        output_path: Path to output raw ATRAC3 file
        bitrate: Bitrate in kbps (128 for LP2, 64 for LP4)
        mode_name: Mode name for logging ("LP2" or "LP4")
        psp_at3tool_path: Optional path to psp_at3tool.exe

    Returns:
        bool: True if conversion successful, False otherwise
    """
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return False

    ext = Path(input_path).suffix.lower()
    if ext not in SUPPORTED_INPUT_FORMATS:
        logger.error(f"Unsupported input format: {ext}")
        return False

    # Create output directory if needed
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    # Check if psp_at3tool is available
    at3tool = get_psp_at3tool_path(psp_at3tool_path)
    use_psp_at3tool = at3tool is not None

    # Create temp directory for intermediate files
    temp_dir = tempfile.mkdtemp(prefix='md_converter_')
    try:
        wav_path = os.path.join(temp_dir, 'intermediate.wav')

        encoder_name = "psp_at3tool" if use_psp_at3tool else "atracdenc"
        logger.info(f"Converting {input_path} to {mode_name} format (ATRAC3 {bitrate}kbps via {encoder_name})...")

        # Step 1: Convert to WAV
        if not _convert_to_wav_intermediate(input_path, wav_path):
            logger.error(f"WAV intermediate conversion failed")
            return False

        # Step 2: Encode to ATRAC3
        if use_psp_at3tool:
            # Use Sony's psp_at3tool (higher quality)
            if not _encode_atrac3_psp(at3tool, wav_path, output_path, bitrate):
                logger.error(f"psp_at3tool ATRAC3 encoding failed")
                return False
        else:
            # Fall back to atracdenc
            oma_path = os.path.join(temp_dir, 'encoded.oma')
            if not _encode_atrac3(wav_path, oma_path, bitrate):
                logger.error(f"atracdenc ATRAC3 encoding failed")
                return False

            # Step 3: Strip OMA header (only needed for atracdenc)
            if not _strip_oma_header(oma_path, output_path):
                logger.error(f"OMA header stripping failed")
                return False

        logger.info(f"✓ {mode_name} conversion complete: {output_path}")
        return True

    except Exception as e:
        logger.error(f"Unexpected error during {mode_name} conversion: {e}")
        return False
    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory: {e}")


def convert_for_lp2(input_path: str, output_path: str, psp_at3tool_path: Optional[str] = None) -> bool:
    """
    Convert audio to ATRAC3 128kbps for LP2 mode upload.

    Uses psp_at3tool.exe if available (higher quality), otherwise falls back to atracdenc.

    Args:
        input_path: Path to input audio file
        output_path: Path to output raw ATRAC3 file
        psp_at3tool_path: Optional path to psp_at3tool.exe for higher quality encoding

    Returns:
        bool: True if conversion successful, False otherwise
    """
    return _convert_lp_common(input_path, output_path, 128, "LP2", psp_at3tool_path)


def convert_for_lp4(input_path: str, output_path: str, psp_at3tool_path: Optional[str] = None) -> bool:
    """
    Convert audio to ATRAC3 64kbps for LP4 mode upload.

    Uses psp_at3tool.exe if available (higher quality), otherwise falls back to atracdenc.

    Args:
        input_path: Path to input audio file
        output_path: Path to output raw ATRAC3 file
        psp_at3tool_path: Optional path to psp_at3tool.exe for higher quality encoding

    Returns:
        bool: True if conversion successful, False otherwise
    """
    return _convert_lp_common(input_path, output_path, 64, "LP4", psp_at3tool_path)


def main():
    """CLI interface for testing the audio converter."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Audio Converter for MiniDisc Batch Burner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check dependencies
  %(prog)s --check-deps

  # Check dependencies with custom psp_at3tool path
  %(prog)s --check-deps --at3tool /path/to/psp_at3tool.exe

  # Get audio duration
  %(prog)s --duration input.mp3

  # Convert to SP mode
  %(prog)s --sp input.mp3 output.raw

  # Convert to LP2 mode (uses psp_at3tool if configured)
  %(prog)s --lp2 input.mp3 output.raw

  # Convert to LP2 mode with explicit psp_at3tool path
  %(prog)s --lp2 --at3tool /path/to/psp_at3tool.exe input.mp3 output.raw

  # Convert to LP4 mode
  %(prog)s --lp4 input.mp3 output.raw

  # Convert with verbose logging
  %(prog)s --sp -v input.mp3 output.raw

Configuration:
  psp_at3tool.exe path can be set via:
    1. --at3tool command line option
    2. PSP_AT3TOOL_PATH environment variable
    3. ~/.md-batch-burner/config.json: {"psp_at3tool_path": "/path/to/psp_at3tool.exe"}
        """
    )

    parser.add_argument('--check-deps', action='store_true',
                        help='Check if required dependencies are installed')
    parser.add_argument('--duration', metavar='FILE',
                        help='Get duration of audio file in seconds')
    parser.add_argument('--sp', nargs=2, metavar=('INPUT', 'OUTPUT'),
                        help='Convert to SP mode (raw PCM)')
    parser.add_argument('--lp2', nargs=2, metavar=('INPUT', 'OUTPUT'),
                        help='Convert to LP2 mode (ATRAC3 128kbps)')
    parser.add_argument('--lp4', nargs=2, metavar=('INPUT', 'OUTPUT'),
                        help='Convert to LP4 mode (ATRAC3 64kbps)')
    parser.add_argument('--at3tool', metavar='PATH',
                        help='Path to psp_at3tool.exe for high-quality ATRAC3 encoding')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable verbose (debug) logging')

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Handle --check-deps
    if args.check_deps:
        print("Checking dependencies...")
        deps = check_dependencies(args.at3tool)
        print()
        for tool in ['ffmpeg', 'ffprobe', 'atracdenc', 'psp_at3tool']:
            available = deps.get(tool, False)
            status = "✓" if available else "✗"
            if tool == 'psp_at3tool' and available:
                path = deps.get('psp_at3tool_path', 'unknown')
                print(f"  {status} {tool} ({path})")
            else:
                print(f"  {status} {tool}")
        print()

        required = ['ffmpeg', 'ffprobe']
        has_required = all(deps.get(r) for r in required)
        has_lp_encoder = deps.get('atracdenc') or deps.get('psp_at3tool')

        if has_required:
            print("✓ Core dependencies available (SP mode ready)")
            if has_lp_encoder:
                encoder = "psp_at3tool" if deps.get('psp_at3tool') else "atracdenc"
                print(f"✓ LP encoder available: {encoder} (LP2/LP4 modes ready)")
            else:
                print("⚠ No LP encoder configured (LP2/LP4 modes unavailable)")
                print("  Install atracdenc or configure psp_at3tool.exe for LP modes")
            return 0
        else:
            missing = [r for r in required if not deps.get(r)]
            print(f"✗ Missing required dependencies: {', '.join(missing)}")
            return 1

    # Handle --duration
    if args.duration:
        try:
            duration = get_audio_duration(args.duration)
            if duration > 0:
                print(f"{duration:.2f} seconds")
                return 0
            else:
                print(f"Error: Unable to determine duration")
                return 1
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            return 1

    # Handle --sp
    if args.sp:
        input_path, output_path = args.sp
        success = convert_for_sp(input_path, output_path)
        return 0 if success else 1

    # Handle --lp2
    if args.lp2:
        input_path, output_path = args.lp2
        success = convert_for_lp2(input_path, output_path, args.at3tool)
        return 0 if success else 1

    # Handle --lp4
    if args.lp4:
        input_path, output_path = args.lp4
        success = convert_for_lp4(input_path, output_path, args.at3tool)
        return 0 if success else 1

    # No action specified
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
