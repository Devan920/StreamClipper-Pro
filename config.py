"""Central configuration for the Stream Clipper pipeline."""

import os
from dataclasses import dataclass, field
from pathlib import Path
import logging


DEFAULT_GEMINI_API_KEY: str = ""


def get_app_data_dir() -> Path:
    """Return %LOCALAPPDATA%/StreamClipperPro (or ~/.streamclipper on non-Windows)."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        p = Path(local_app_data) / "StreamClipperPro"
    else:
        p = Path.home() / ".streamclipper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_segments_dir() -> Path:
    """Return %LOCALAPPDATA%/StreamClipperPro/segments."""
    p = get_app_data_dir() / "segments"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_default_output_dir() -> Path:
    """Return %USERPROFILE%/Videos/StreamClipper Pro."""
    p = Path.home() / "Videos" / "StreamClipper Pro"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class IngestorConfig:
    """Configuration for the stream ingestor module."""

    segment_duration: int = 10  # 10s rolling recording chunks for sliding window
    max_segments: int = 60  # keep last 60 segments (~10 min rolling buffer)
    output_dir: Path = field(default_factory=get_segments_dir)
    format: str = "ts"  # MPEG-TS (.ts) segments for robust live streaming
    video_quality: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080][protocol^=m3u8]/best[protocol^=m3u8]/best"  # Prefer up to 1080p
    ffmpeg_loglevel: str = "warning"


@dataclass
class HypeConfig:
    """Configuration for hype/highlight detection."""

    chat_window: int = 10  # seconds to aggregate chat messages
    chat_spike_threshold: float = 3.0  # multiplier over rolling average
    rms_window: float = 0.5  # seconds for RMS energy window
    rms_spike_threshold: float = 2.5  # multiplier over rolling average RMS
    min_hype_gap: float = 10.0  # minimum seconds between hype events
    lookback_seconds: float = 10.0  # seconds before peak to include
    lookahead_seconds: float = 50.0  # seconds after peak to include
    clip_duration: int = 60  # target clip duration in seconds (60, 45, 30, 15)
    clip_interval_seconds: int = 10  # 10s, 30s, or 0 (0 = Hype Spike Only)
    clip_interval_name: str = "Every 10s (Rapid Sliding Window)"


@dataclass
class TranscriberConfig:
    """Configuration for the transcription module."""

    model_size: str = "small"  # Default upgraded to "small" for high accuracy
    device: str = "auto"  # "cpu", "cuda", or "auto"
    compute_type: str = "int8"  # "int8", "float16", "auto"
    cpu_threads: int = 8
    language: str | None = None  # None = auto-detect
    beam_size: int = 5
    best_of: int = 5
    condition_on_previous_text: bool = False
    word_timestamps: bool = True
    vad_filter: bool = True
    vad_min_silence_duration_ms: int = 400
    accuracy_mode: str = "Balanced (Whisper Small - Fast & Accurate)"  # "Balanced...", "High Accuracy...", "Ultra AI..."
    gemini_api_key: str | None = DEFAULT_GEMINI_API_KEY
    stream_title: str = ""


@dataclass
class EditorConfig:
    """Configuration for the video editor module."""

    output_dir: Path = field(default_factory=get_default_output_dir)
    aspect_ratio: tuple[int, int] = (9, 16)
    aspect_ratio_name: str = "9:16 (Shorts / Reels)"
    output_width: int = 1080
    output_height: int = 1920
    theme: str = "Viral Glow Pop (Shorts Edit)"  # Default preset: Viral Glow Pop (Shorts Edit)
    font_name: str = "Arial Black"
    font_size: int = 78
    font_scale: float = 1.0
    font_color: str = "&H00FFFFFF"
    highlight_color: str = "&H0014FF39"  # Neon Green (ASS BGR)
    outline_color: str = "&H00000000"
    outline_width: int = 4
    shadow_color: str = "&H00000000"
    shadow_offset: int = 2
    margin_v: int = 640  # Sweet spot: 640px for 9:16 (~65% down)
    words_per_line: int = 3  # 1-3 words max per punchy dynamic chunk
    max_gap_seconds: float = 0.45
    uppercase: bool = True
    background_box: bool = False
    background_box_color: str = "&H80000000"
    caption_y_position: float = 0.75
    enable_face_tracking: bool = True  # Smart face tracking for 9:16, 4:5, 1:1
    layout_mode: str = "Smart Face Center (Single Frame)"
    facecam_corner: str = "Auto-Detect Facecam"
    quality: str = "1080p Ultra (High Bitrate)"  # "1080p Ultra (High Bitrate)" | "1080p Standard" | "720p Fast"
    crf: int = 17
    preset: str = "medium"
    audio_bitrate: str = "192k"


@dataclass
class PipelineConfig:
    """Master configuration combining all module configs."""

    ingestor: IngestorConfig = field(default_factory=IngestorConfig)
    hype: HypeConfig = field(default_factory=HypeConfig)
    transcriber: TranscriberConfig = field(default_factory=TranscriberConfig)
    editor: EditorConfig = field(default_factory=EditorConfig)
    gemini_api_key: str | None = DEFAULT_GEMINI_API_KEY
    log_level: str = "INFO"

    def setup_logging(self) -> None:
        """Configure root logger with the pipeline's log level."""
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%H:%M:%S",
        )
