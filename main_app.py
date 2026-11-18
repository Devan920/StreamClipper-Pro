#!/usr/bin/env python3
"""
Stream Clipper Pro — PySide6 Desktop Application

A modern dark-themed GUI for real-time YouTube Live stream clipping.
Wraps the backend modules (stream_ingestor, hype_detector, transcriber,
video_editor) with QThread workers so the UI stays 100 % responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Ensure local package imports and runtime binaries (ffmpeg, ffprobe) work
# ---------------------------------------------------------------------------
BASE_DIR = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE_DIR))

# Augment PATH with local runtime dirs where ffmpeg / ffprobe might live
_extra_paths = [
    str(BASE_DIR),
    str(Path(sys.executable).parent),
    os.path.expandvars(r"%USERPROFILE%\.spotiflac"),
    r"C:\Program Files\Topaz Labs LLC\Topaz Video AI",
]
for _p in _extra_paths:
    if os.path.isdir(_p) and _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")


def get_app_icon_path() -> Path | None:
    """Resolve assets/app_icon.ico safely across source and PyInstaller environments."""
    candidates = [
        Path(BASE_DIR) / "assets" / "app_icon.ico",
        Path(BASE_DIR) / "assets" / "icon.ico",
        Path(__file__).resolve().parent / "assets" / "app_icon.ico",
        Path(sys.executable).parent / "assets" / "app_icon.ico" if getattr(sys, "frozen", False) else None,
    ]
    for c in candidates:
        if c and c.is_file():
            return c
    return None

from config import (
    DEFAULT_GEMINI_API_KEY,
    EditorConfig,
    HypeConfig,
    IngestorConfig,
    PipelineConfig,
    TranscriberConfig,
    get_app_data_dir,
    get_default_output_dir,
    get_segments_dir,
)
from vod_processor import (
    SUPPORTED_VIDEO_EXTENSIONS,
    detect_input_source,
    download_youtube_vod,
    generate_sliding_window_moments,
    get_subprocess_kwargs,
    get_video_duration,
    scan_audio_peaks,
)
from video_renderer import concat_segments

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Stream Clipper Pro"
APP_VERSION = "1.0.0"

ASPECT_RATIOS: dict[str, dict[str, Any]] = {
    "9:16 (Shorts / Reels)": {
        "width": 1080,
        "height": 1920,
        "margin_v": 640,  # Sweet spot: 640px from bottom (~65% down, clears face & UI)
        "font_scale": 1.0,
        "badge": "9:16",
    },
    "16:9 (YouTube Landscape)": {
        "width": 1920,
        "height": 1080,
        "margin_v": 240,  # Lower-third sweet spot: 240px from bottom
        "font_scale": 0.75,
        "badge": "16:9",
    },
    "1:1 (Square Post)": {
        "width": 1080,
        "height": 1080,
        "margin_v": 340,  # Lower-third sweet spot: 340px from bottom
        "font_scale": 0.82,
        "badge": "1:1",
    },
    "4:5 (Instagram Portrait)": {
        "width": 1080,
        "height": 1350,
        "margin_v": 430,  # Lower-third sweet spot: 430px from bottom
        "font_scale": 0.90,
        "badge": "4:5",
    },
}

QUALITY_PROFILES: dict[str, dict[str, Any]] = {
    "1080p Ultra (High Bitrate)": {
        "scale_factor": 1.0,
        "crf": 17,
        "preset": "medium",
        "audio_bitrate": "192k",
    },
    "1080p Standard": {
        "scale_factor": 1.0,
        "crf": 21,
        "preset": "fast",
        "audio_bitrate": "160k",
    },
    "720p Fast": {
        "scale_factor": 720.0 / 1080.0,
        "crf": 23,
        "preset": "veryfast",
        "audio_bitrate": "128k",
    },
}

SETTINGS_DIR = get_app_data_dir()
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
DEFAULT_OUTPUT_DIR = get_default_output_dir()


def _load_settings() -> dict[str, Any]:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not data.get("gemini_api_key"):
                data["gemini_api_key"] = DEFAULT_GEMINI_API_KEY
            return data
        # Fallback to legacy path if it exists
        legacy = Path.home() / ".streamclipper" / "settings.json"
        if legacy.is_file():
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if not data.get("gemini_api_key"):
                data["gemini_api_key"] = DEFAULT_GEMINI_API_KEY
            _save_settings(data)
            return data
    except Exception:
        pass
    return {"gemini_api_key": DEFAULT_GEMINI_API_KEY}


def _save_settings(data: dict[str, Any]) -> None:
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        current = _load_settings()
        current.update(data)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass

CAPTION_STYLES: dict[str, dict[str, Any]] = {
    "Viral Glow Pop (Shorts Edit)": {
        "theme": "Viral Glow Pop (Shorts Edit)",
        "font_name": "Montserrat Black",
        "font_size": 78,
        "outline_width": 4,
        "shadow_offset": 2,
        "highlight_color": "&H0014FF39",  # Neon Green (ASS BGR)
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 640,
        "words_per_line": 3,
        "background_box": False,
    },
    "Hormozi Pop (Classic)": {
        "theme": "Hormozi Pop (Classic)",
        "font_name": "Arial Black",
        "font_size": 78,
        "outline_width": 5,
        "shadow_offset": 3,
        "highlight_color": "&H0000D7FF",  # Bright Yellow (ASS BGR)
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 3,
        "background_box": False,
    },
    "MrBeast Viral (Green)": {
        "theme": "MrBeast Viral (Green)",
        "font_name": "Arial Black",
        "font_size": 80,
        "outline_width": 6,
        "shadow_offset": 3,
        "highlight_color": "&H0039FF14",  # Bright Neon Lime Green
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 3,
        "background_box": False,
    },
    "Cyberpunk Neon (Gaming)": {
        "theme": "Cyberpunk Neon (Gaming)",
        "font_name": "Arial Black",
        "font_size": 74,
        "outline_width": 4,
        "shadow_offset": 2,
        "highlight_color": "&H00FFFFFF",  # Active turns pure white
        "font_color": "&H00FFFF00",       # Electric Cyan base
        "outline_color": "&H00FF00AA",     # Neon Magenta border
        "margin_v": 380,
        "words_per_line": 3,
        "background_box": False,
    },
    "Fire Hype (Red/Orange)": {
        "theme": "Fire Hype (Red/Orange)",
        "font_name": "Arial Black",
        "font_size": 78,
        "outline_width": 5,
        "shadow_offset": 3,
        "highlight_color": "&H000045FF",  # Vibrant Fire Orange-Red
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 3,
        "background_box": False,
    },
    "Podcast Box (Clean)": {
        "theme": "Podcast Box (Clean)",
        "font_name": "Arial",
        "font_size": 68,
        "outline_width": 2,
        "shadow_offset": 0,
        "highlight_color": "&H0000C8FF",  # Warm Amber/Yellow
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 4,
        "background_box": True,
        "background_box_color": "&H99000000",
    },
    "Gold Luxury (Aesthetic)": {
        "theme": "Gold Luxury (Aesthetic)",
        "font_name": "Arial Black",
        "font_size": 72,
        "outline_width": 2,
        "shadow_offset": 4,
        "highlight_color": "&H00FFFFFF",  # Pure bright white
        "font_color": "&H0037AFD4",       # Metallic Gold base
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 4,
        "background_box": False,
    },
    "Minimalist Clean": {
        "theme": "Minimalist Clean",
        "font_name": "Segoe UI",
        "font_size": 66,
        "outline_width": 1.5,
        "shadow_offset": 2,
        "highlight_color": "&H00FFFFFF",
        "font_color": "&H00F5F5F5",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 4,
        "background_box": False,
    },
    "Karaoke Sweep": {
        "theme": "Karaoke Sweep",
        "font_name": "Arial Black",
        "font_size": 74,
        "outline_width": 4,
        "shadow_offset": 2,
        "highlight_color": "&H0000D7FF",  # Yellow sweep fill
        "font_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "margin_v": 380,
        "words_per_line": 4,
        "background_box": False,
    },
}

LANGUAGES: dict[str, str | None] = {
    "Auto Detect": None,
    "English": "en",
    "Malayalam": "ml",
    "Hindi": "hi",
    "Tamil": "ta",
    "Telugu": "te",
    "Japanese": "ja",
    "Korean": "ko",
}

# ---------------------------------------------------------------------------
# Creative Studio Obsidian Stylesheet
# ---------------------------------------------------------------------------

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0D0F12;
    color: #F3F4F6;
    font-family: 'Segoe UI', 'Inter', -apple-system, sans-serif;
    font-size: 13px;
}
QFrame#panelCard {
    background-color: #15181E;
    border: 1px solid #242933;
    border-radius: 10px;
}
QFrame#headerBar {
    background-color: #11141A;
    border-bottom: 1px solid #242933;
}
QLineEdit {
    background-color: #181C24;
    border: 1px solid #28303E;
    border-radius: 8px;
    padding: 7px 12px;
    color: #F3F4F6;
    font-size: 13px;
    selection-background-color: #6366F1;
}
QLineEdit:hover {
    border-color: #374151;
}
QLineEdit:focus {
    border: 1px solid #6366F1;
    background-color: #1C212C;
}
QLineEdit:read-only {
    background-color: #11141A;
    color: #9CA3AF;
    border: 1px solid #242933;
}
QCheckBox {
    color: #F3F4F6;
    font-size: 12px;
    font-weight: 600;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #374151;
    background-color: #181C24;
}
QCheckBox::indicator:hover {
    border-color: #6366F1;
    background-color: #1F2430;
}
QCheckBox::indicator:checked {
    background-color: #6366F1;
    border-color: #6366F1;
}
QComboBox {
    background-color: #181C24;
    border: 1px solid #28303E;
    border-radius: 8px;
    padding: 6px 12px;
    color: #F3F4F6;
    font-size: 12px;
    min-width: 100px;
}
QComboBox:hover {
    border-color: #374151;
}
QComboBox:focus {
    border-color: #6366F1;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #9CA3AF;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: #181C24;
    border: 1px solid #28303E;
    color: #F3F4F6;
    selection-background-color: #6366F1;
    selection-color: #FFFFFF;
    outline: none;
    padding: 4px;
}
QPushButton {
    background-color: #1F242E;
    border: 1px solid #2E3646;
    border-radius: 8px;
    padding: 6px 14px;
    color: #F3F4F6;
    font-weight: 600;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #28303E;
    border-color: #4B5563;
}
QPushButton:pressed {
    background-color: #15181E;
}
QPushButton:disabled {
    background-color: #15181E;
    border-color: #242933;
    color: #4B5563;
}
QProgressBar {
    background-color: #11141A;
    border: 1px solid #242933;
    border-radius: 6px;
    text-align: center;
    color: #FFFFFF;
    font-weight: bold;
    min-height: 20px;
}
QProgressBar::chunk {
    border-radius: 5px;
    background-color: #10B981;
}
QTextEdit {
    background-color: #0B0D11;
    border: 1px solid #242933;
    border-radius: 8px;
    color: #E5E7EB;
    font-family: 'Consolas', 'Cascadia Code', monospace;
    font-size: 11px;
    padding: 8px;
}
QScrollArea {
    background-color: transparent;
    border: none;
}
QScrollBar:vertical {
    background-color: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #28303E;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background-color: #4B5563;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background-color: transparent;
    height: 8px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: #28303E;
    border-radius: 4px;
    min-width: 24px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #4B5563;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QLabel {
    color: #F3F4F6;
}
QStatusBar {
    background-color: #090A0D;
    color: #9CA3AF;
    font-size: 11px;
    border-top: 1px solid #1E222A;
    padding: 2px 8px;
}
"""

# ---------------------------------------------------------------------------
# Log colour map for the activity console
# ---------------------------------------------------------------------------

LOG_COLOURS: dict[str, str] = {
    "INFO": "#D1D5DB",
    "WARNING": "#FBBF24",
    "ERROR": "#F87171",
    "HYPE": "#FB923C",
    "DEBUG": "#6B7280",
    "SUCCESS": "#34D399",
}


# ===================================================================
# Custom Qt Logging Handler
# ===================================================================

class _GuiLogSignal(QThread):
    """Tiny QObject living on the main thread to relay log signals."""
    message = Signal(str, str)  # (formatted_message, level_name)


class GuiLogHandler(logging.Handler):
    """Logging handler that emits Qt signals for each log record.

    Install via ``logging.getLogger().addHandler(GuiLogHandler(signal_obj))``.
    """

    def __init__(self, signal_emitter: _GuiLogSignal) -> None:
        super().__init__()
        self._emitter = signal_emitter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            msg = f"[{ts}] {record.getMessage()}"
            level = record.levelname
            self._emitter.message.emit(msg, level)
        except Exception:
            self.handleError(record)


# ===================================================================
# Pipeline Worker (QThread)
# ===================================================================

@dataclass
class _RenderJob:
    """Internal job payload for the background render queue."""
    clip_index: int
    source_path: Path
    start: float
    end: float
    score: float
    output_name: str
    is_temp_file: bool = False


class PipelineWorker(QThread):
    """Runs the full ingest → detect → transcribe → edit pipeline in a
    background thread with its own asyncio event loop."""

    # --- Signals ----------------------------------------------------------
    status_changed = Signal(str)
    log_message = Signal(str, str)        # (message, level)
    segment_processed = Signal(int)       # total segment count
    hype_detected = Signal(float, float)  # (timestamp, score)
    hype_meter_update = Signal(float)     # 0.0 – 1.0
    clip_ready = Signal(str, float, float, float, dict)  # path, start, end, score, title_info
    error_occurred = Signal(str)
    progress_update = Signal(int, int)    # (current, total) for VOD mode

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode: str = "live"  # "live" | "vod"
        self._url: str = ""
        self._config: PipelineConfig = PipelineConfig()
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- Configuration (call before start()) -----------------------------

    def configure(
        self,
        mode: str,
        url: str,
        config: PipelineConfig,
    ) -> None:
        self._mode = mode
        self._url = url
        self._config = config
        self._stop_requested = False

    # --- Thread entry point ----------------------------------------------

    def run(self) -> None:  # noqa: D401
        """Thread body — creates an event loop and runs the pipeline."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            if self._mode == "live":
                self._loop.run_until_complete(self._run_live())
            else:
                self._loop.run_until_complete(self._run_vod())
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self.status_changed.emit("idle")

    # --- Stop request ----------------------------------------------------

    def request_stop(self) -> None:
        self._stop_requested = True
        # Interrupt the event loop if it's running
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # --- Background Render Worker Loop -----------------------------------

    def _render_worker_loop(
        self,
        render_queue: queue.Queue,
        transcriber: Any,
        editor: Any,
        gemini_key: str | None,
    ) -> None:
        """Processes render jobs asynchronously so stream recording never stalls."""
        while not self._stop_requested:
            try:
                job: _RenderJob | None = render_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if job is None:
                render_queue.task_done()
                break

            try:
                self.status_changed.emit("rendering")
                self.log_message.emit(
                    f"Processing Clip {job.clip_index} [{job.start:.1f}s – {job.end:.1f}s] in background queue…",
                    "INFO",
                )

                # Transcribe
                self.status_changed.emit("transcribing")
                try:
                    transcript = transcriber.transcribe_segment(
                        job.source_path, job.start, job.end
                    )
                except Exception as exc:
                    self.log_message.emit(f"Transcription warning on Clip {job.clip_index}: {exc}", "WARNING")
                    from transcriber import Transcript
                    transcript = Transcript()

                # Render vertical clip with animated captions
                self.status_changed.emit("rendering")
                output_path = editor.create_clip(
                    source_path=job.source_path,
                    start=job.start,
                    end=job.end,
                    transcript=transcript,
                    output_name=job.output_name,
                )

                # Viral English Title & Hashtags
                from title_generator import generate_clip_title, save_clip_metadata
                transcript_text = " ".join(seg.text for seg in transcript.segments) if transcript and getattr(transcript, "segments", None) else ""
                title_info = generate_clip_title(
                    transcript_text=transcript_text,
                    stream_title=self._url,
                    gemini_api_key=gemini_key,
                    hype_score=job.score * 100.0,
                )
                save_clip_metadata(output_path, title_info)

                self.clip_ready.emit(
                    str(output_path), job.start, job.end, job.score, title_info,
                )
                self.status_changed.emit("clip_ready")
                self.log_message.emit(
                    f"✅ Clip {job.clip_index} saved: {output_path.name} — \"{title_info.get('title', '')}\"",
                    "SUCCESS",
                )
            except Exception as exc:
                self.log_message.emit(f"Render error on Clip {job.clip_index}: {exc}", "ERROR")
            finally:
                if job.is_temp_file:
                    try:
                        job.source_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                render_queue.task_done()

    # --- Live pipeline ---------------------------------------------------

    async def _run_live(self) -> None:
        from hype_detector import HypeDetector
        from stream_ingestor import StreamIngestor
        from transcriber import Transcriber
        from video_editor import VideoEditor

        self.status_changed.emit("buffering")
        self.log_message.emit("Starting live stream monitoring…", "INFO")

        ingestor = StreamIngestor(
            self._url,
            self._config.ingestor,
            log_callback=lambda msg, lvl: self.log_message.emit(msg, lvl),
        )
        detector = HypeDetector(self._config.hype)
        transcriber = Transcriber(self._config.transcriber)
        editor = VideoEditor(self._config.editor)
        gemini_key = getattr(self._config, "gemini_api_key", None)

        # Start dedicated background render thread so stream recording never stalls
        render_queue: queue.Queue = queue.Queue(maxsize=50)
        render_thread = threading.Thread(
            target=self._render_worker_loop,
            args=(render_queue, transcriber, editor, gemini_key),
            daemon=True,
        )
        render_thread.start()

        # Try to start chat analyser (best-effort)
        video_id = _extract_video_id(self._url)
        if video_id:
            detector.chat_analyzer.start(video_id)
            self.log_message.emit(f"Chat analyser started for {video_id}", "INFO")

        clip_dur = float(self._config.hype.clip_duration or 60.0)
        interval_sec = getattr(self._config.hype, "clip_interval_seconds", 10)
        seg_dur = float(self._config.ingestor.segment_duration or 10.0)

        n_needed = max(1, int(round(clip_dur / max(1.0, seg_dur))))
        step_segments = max(1, int(round(interval_sec / max(1.0, seg_dur)))) if interval_sec > 0 else 1

        buffered_segments: deque[Path] = deque(maxlen=60)
        segments_since_last_trigger = 0
        segment_count = 0
        clip_count = 0

        try:
            async for segment_path in ingestor.start():
                if self._stop_requested:
                    break

                segment_count += 1
                buffered_segments.append(segment_path)
                segments_since_last_trigger += 1
                self.segment_processed.emit(segment_count)

                # Buffer status log
                buffered_secs = len(buffered_segments) * seg_dur
                self.log_message.emit(
                    f"Live buffer: {len(buffered_segments)}/{n_needed} segments (~{buffered_secs:.0f}s / {clip_dur:.0f}s)",
                    "INFO",
                )

                if interval_sec > 0:
                    # Sliding Window Mode (Every 10s / Every 30s)
                    if len(buffered_segments) >= n_needed and segments_since_last_trigger >= step_segments:
                        segments_since_last_trigger = 0
                        clip_count += 1
                        clip_segments = list(buffered_segments)[-n_needed:]

                        self.log_message.emit(
                            f"⚡ Sliding window triggered Clip {clip_count:02d} ({clip_dur:.0f}s duration) — queueing render!",
                            "HYPE",
                        )
                        self.hype_meter_update.emit(0.85)

                        # Concat into temporary slice
                        temp_concat = self._config.ingestor.output_dir / f"live_window_{clip_count:03d}_{int(time.time())}.mp4"
                        concat_segments(clip_segments, temp_concat, max_duration=clip_dur)

                        job = _RenderJob(
                            clip_index=clip_count,
                            source_path=temp_concat,
                            start=0.0,
                            end=clip_dur,
                            score=0.88,
                            output_name=f"live_clip_{clip_count:03d}_{int(time.time())}",
                            is_temp_file=True,
                        )
                        render_queue.put(job)
                else:
                    # Hype Spike Only mode
                    seg_start_time = (segment_count - 1) * seg_dur
                    try:
                        events = await detector.analyze_segment(segment_path, seg_start_time)
                    except Exception as exc:
                        self.log_message.emit(f"Analysis error: {exc}", "ERROR")
                        events = []

                    if events:
                        best = max(events, key=lambda e: e.score)
                        self.hype_meter_update.emit(min(1.0, best.score))
                        for event in events:
                            if self._stop_requested:
                                break
                            clip_count += 1
                            self.status_changed.emit("hype_detected")
                            self.hype_detected.emit(event.timestamp, event.score)
                            self.log_message.emit(
                                f"🔥 HYPE at {event.timestamp:.1f}s (score {event.score:.2f}) — queueing render",
                                "HYPE",
                            )

                            seg_clip_start = max(0.0, event.segment_offset - self._config.hype.lookback_seconds)
                            seg_clip_end = min(seg_dur, event.segment_offset + self._config.hype.lookahead_seconds)
                            job = _RenderJob(
                                clip_index=clip_count,
                                source_path=segment_path,
                                start=seg_clip_start,
                                end=seg_clip_end,
                                score=event.score,
                                output_name=f"live_clip_{clip_count:03d}_t{int(event.timestamp)}",
                                is_temp_file=False,
                            )
                            render_queue.put(job)
                    else:
                        self.hype_meter_update.emit(0.0)

                if not self._stop_requested:
                    self.status_changed.emit("buffering")
        finally:
            detector.chat_analyzer.stop()
            await ingestor.stop()
            render_queue.put(None)
            render_thread.join(timeout=3.0)
            self.log_message.emit(
                f"Session ended — {segment_count} segments received, {clip_count} clips dispatched to render.", "INFO",
            )

    # --- VOD / Local Video pipeline --------------------------------------

    async def _run_vod(self) -> None:
        from transcriber import Transcriber
        from video_renderer import VideoRenderer
        from title_generator import generate_clip_title, save_clip_metadata

        src_info = detect_input_source(self._url)
        temp_dl_dir: Path | None = None

        try:
            if src_info["type"] == "local_file" and src_info["path"] and Path(src_info["path"]).is_file():
                vod_path = Path(src_info["path"])
                video_title = src_info.get("title", vod_path.stem)
                self.log_message.emit(f"Processing local video: {vod_path.name}", "INFO")
            else:
                self.status_changed.emit("downloading")
                self.log_message.emit(f"Downloading YouTube video: {self._url}", "INFO")
                temp_dl_dir = Path(tempfile.mkdtemp(prefix="scp_vod_"))
                vod_path, video_title = download_youtube_vod(
                    self._url,
                    temp_dl_dir,
                    log_callback=lambda msg, lvl: self.log_message.emit(msg, lvl),
                )
                self.log_message.emit(f"Downloaded: {video_title} ({vod_path.stat().st_size / 1e6:.1f} MB)", "SUCCESS")

            if self._stop_requested:
                return

            duration = get_video_duration(vod_path)
            clip_dur = float(self._config.hype.clip_duration or 60.0)
            interval_sec = getattr(self._config.hype, "clip_interval_seconds", 10)
            self.log_message.emit(f"Duration: {duration:.1f}s | Target Clip Duration: {clip_dur:.0f}s", "INFO")

            self.status_changed.emit("analyzing")
            if interval_sec > 0:
                # Sliding Window Mode (Every 10s / Every 30s)
                self.log_message.emit(
                    f"Generating sliding window clips every {interval_sec}s ({clip_dur:.0f}s duration)…",
                    "INFO",
                )
                moments = generate_sliding_window_moments(
                    duration, clip_duration=clip_dur, step_interval=float(interval_sec)
                )
                self.log_message.emit(
                    f"Generated {len(moments)} sliding-window clip moments! Beginning render pipeline…",
                    "SUCCESS",
                )
            else:
                # Fast Audio Peak Scanning (Hype Spike Only)
                self.log_message.emit("Scanning full audio track for peak energy highlight moments…", "INFO")

                num_clips = 4
                if duration >= 600:
                    num_clips = 5
                elif duration < 120:
                    num_clips = min(3, max(1, int(duration // clip_dur)))

                peaks = scan_audio_peaks(
                    video_path=vod_path,
                    clip_duration=clip_dur,
                    top_k=num_clips,
                )

                if not peaks:
                    self.log_message.emit("No audio peaks detected; using fallback moments", "WARNING")
                    from vod_processor import _build_fallback_moments
                    peaks = _build_fallback_moments(duration, clip_dur, count=num_clips)

                moments = peaks
                self.log_message.emit(f"Found {len(peaks)} high-energy highlight moments! Beginning render pipeline…", "SUCCESS")

            self._config.transcriber.stream_title = video_title or Path(vod_path).stem
            transcriber = Transcriber(self._config.transcriber)
            renderer = VideoRenderer(self._config.editor)
            gemini_key = getattr(self._config, "gemini_api_key", None)

            for i, (clip_start, clip_end, score) in enumerate(moments, 1):
                if self._stop_requested:
                    return

                self.progress_update.emit(i, len(moments))
                self.hype_detected.emit(clip_start, score)
                self.hype_meter_update.emit(min(1.0, score))

                self.status_changed.emit("transcribing")
                self.log_message.emit(
                    f"Transcribing Clip {i}/{len(moments)} [{clip_start:.1f}s – {clip_end:.1f}s] (Hype {score:.0%})…",
                    "INFO",
                )

                try:
                    transcript = transcriber.transcribe_segment(vod_path, clip_start, clip_end)
                except Exception as exc:
                    self.log_message.emit(f"Transcription error in clip {i}: {exc}", "WARNING")
                    from transcriber import Transcript
                    transcript = Transcript()

                # Render with face tracking & ASS captions
                self.status_changed.emit("rendering")
                self.log_message.emit(f"Rendering Clip {i}/{len(moments)} with animated captions…", "INFO")
                try:
                    stem = f"clip_{i:02d}_t{int(clip_start)}"
                    output_path = renderer.create_clip(
                        source_path=vod_path,
                        start=clip_start,
                        end=clip_end,
                        transcript=transcript,
                        output_name=stem,
                    )

                    # Generate viral English title & hashtags
                    transcript_text = " ".join(seg.text for seg in transcript.segments) if transcript and getattr(transcript, "segments", None) else ""
                    title_info = generate_clip_title(
                        transcript_text=transcript_text,
                        stream_title=video_title or self._url,
                        gemini_api_key=gemini_key,
                        hype_score=score * 100.0,
                    )
                    save_clip_metadata(output_path, title_info)

                    self.clip_ready.emit(
                        str(output_path), clip_start, clip_end, score, title_info,
                    )
                    self.status_changed.emit("clip_ready")
                    self.log_message.emit(f"✅ Clip {i}/{len(moments)} saved: {output_path.name} — \"{title_info.get('title', '')}\"", "SUCCESS")
                except Exception as exc:
                    self.log_message.emit(f"Render error in clip {i}: {exc}", "ERROR")

            self.status_changed.emit("completed")
            self.log_message.emit(f"🎉 Processing complete! Generated {len(moments)} clips.", "SUCCESS")

        finally:
            if temp_dl_dir and temp_dl_dir.is_dir():
                try:
                    shutil.rmtree(temp_dl_dir, ignore_errors=True)
                except Exception:
                    pass


# ===================================================================
# Thumbnail Worker
# ===================================================================

class ThumbnailWorker(QThread):
    """Extracts a thumbnail from a video clip in the background."""
    thumbnail_ready = Signal(str, QPixmap)  # (clip_path, pixmap)

    def __init__(self, clip_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clip_path = clip_path

    def run(self) -> None:
        try:
            cmd = [
                "ffmpeg", "-i", self._clip_path,
                "-vframes", "1",
                "-vf", "scale=280:-1",
                "-f", "image2pipe",
                "-vcodec", "png",
                "-",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=True,
                **get_subprocess_kwargs(),
            )
            pixmap = QPixmap()
            pixmap.loadFromData(result.stdout, "PNG")
            if not pixmap.isNull():
                self.thumbnail_ready.emit(self._clip_path, pixmap)
        except Exception:
            pass  # thumbnail is optional


# ===================================================================
# Clip Card Widget (Creative Studio Layout)
# ===================================================================

class ClipCard(QFrame):
    """A sleek clip entry in the gallery panel with English viral title and action buttons."""

    clip_deleted = Signal(str)

    def __init__(
        self,
        clip_path: str,
        start_time: float,
        end_time: float,
        hype_score: float,
        title_info: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.clip_path = clip_path
        self.title_info = title_info or self._load_or_generate_title(clip_path, hype_score)
        self.setObjectName("clipCard")
        self.setStyleSheet(
            """
            QFrame#clipCard {
                background-color: #15181E;
                border: 1px solid #242933;
                border-radius: 8px;
            }
            QFrame#clipCard:hover {
                border-color: #6366F1;
                background-color: #181C24;
            }
            """
        )
        self.setFixedHeight(176)
        self._build_ui(clip_path, start_time, end_time, hype_score)

        # Kick off thumbnail extraction
        self._thumb_worker = ThumbnailWorker(clip_path, self)
        self._thumb_worker.thumbnail_ready.connect(self._on_thumbnail)
        self._thumb_worker.start()

    def _load_or_generate_title(self, clip_path: str, hype_score: float) -> dict[str, str]:
        txt_path = Path(clip_path).with_suffix(".txt")
        if txt_path.is_file():
            try:
                content = txt_path.read_text(encoding="utf-8")
                t_match = re.search(r"Title\s*:\s*(.+)", content, re.IGNORECASE)
                h_match = re.search(r"Hashtags\s*:\s*(.+)", content, re.IGNORECASE)
                f_match = re.search(r"Full\s*:\s*(.+)", content, re.IGNORECASE)
                title = t_match.group(1).strip() if t_match else Path(clip_path).stem
                tags = h_match.group(1).strip() if h_match else "#Shorts #Viral"
                full = f_match.group(1).strip() if f_match else f"{title} {tags}"
                return {
                    "title": title,
                    "hashtags": tags,
                    "full_title": full,
                    "source": "saved_txt",
                }
            except Exception:
                pass
        try:
            from title_generator import generate_offline_title, save_clip_metadata
            res = generate_offline_title(stream_title=Path(clip_path).stem, hype_score=hype_score * 100.0)
            save_clip_metadata(Path(clip_path), res)
            return res
        except Exception:
            fallback = Path(clip_path).stem.replace("_", " ").title()
            return {
                "title": fallback,
                "hashtags": "#Shorts #Viral #Highlights",
                "full_title": f"{fallback} #Shorts #Viral #Highlights",
                "source": "fallback",
            }

    def _build_ui(
        self,
        clip_path: str,
        start_time: float,
        end_time: float,
        hype_score: float,
    ) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # Thumbnail (9:16 vertical ratio preview)
        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(92, 150)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setStyleSheet(
            "background-color: #0D0F12; border-radius: 6px; color: #4B5563; font-size: 11px;"
        )
        self._thumb_label.setText("Preview…")
        layout.addWidget(self._thumb_label)

        # Info column
        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(3)

        # Row 1: Score badge & timecode
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)

        score_val = max(0.0, min(1.0, hype_score))
        if score_val >= 0.7:
            badge_bg = "rgba(239, 68, 68, 0.15)"
            badge_fg = "#F87171"
            badge_bd = "rgba(239, 68, 68, 0.35)"
        elif score_val >= 0.4:
            badge_bg = "rgba(245, 158, 11, 0.15)"
            badge_fg = "#FBBF24"
            badge_bd = "rgba(245, 158, 11, 0.35)"
        else:
            badge_bg = "rgba(16, 185, 129, 0.15)"
            badge_fg = "#34D399"
            badge_bd = "rgba(16, 185, 129, 0.35)"

        score_badge = QLabel(f"🔥 {score_val:.0%}")
        score_badge.setStyleSheet(
            f"background-color: {badge_bg}; color: {badge_fg}; "
            f"border: 1px solid {badge_bd}; border-radius: 8px; "
            f"padding: 1px 6px; font-size: 10px; font-weight: bold;"
        )
        score_badge.setFixedHeight(18)
        meta_row.addWidget(score_badge)

        dur = max(0.0, end_time - start_time)
        if dur > 0:
            time_text = f"⏱ {_fmt_time(start_time)} → {_fmt_time(end_time)} ({dur:.0f}s)"
        else:
            time_text = "⏱ Ready to play"
        time_label = QLabel(time_text)
        time_label.setStyleSheet("color: #9CA3AF; font-size: 10px;")
        meta_row.addWidget(time_label, 1)
        info_layout.addLayout(meta_row)

        # Row 2: Viral English Title
        title_text = self.title_info.get("title", Path(clip_path).name)
        self._title_label = QLabel(title_text)
        self._title_label.setWordWrap(True)
        self._title_label.setMaximumHeight(38)
        self._title_label.setStyleSheet("color: #F3F4F6; font-size: 11px; font-weight: bold; line-height: 1.25;")
        self._title_label.setToolTip(title_text)
        info_layout.addWidget(self._title_label)

        # Row 3: Trending Hashtags
        tags_text = self.title_info.get("hashtags", "#Shorts #Viral")
        self._tags_label = QLabel(tags_text)
        self._tags_label.setStyleSheet("color: #818CF8; font-size: 10px; font-weight: 600;")
        self._tags_label.setToolTip(tags_text)
        info_layout.addWidget(self._tags_label)

        info_layout.addStretch()

        # Row 4: Action Buttons (Play, Copy Title, Open Folder, Delete)
        btn_vbox = QVBoxLayout()
        btn_vbox.setContentsMargins(0, 4, 0, 0)
        btn_vbox.setSpacing(4)

        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(5)

        play_btn = QPushButton("▶ Play")
        play_btn.setFixedHeight(24)
        play_btn.setStyleSheet(
            "QPushButton { background-color: #4F46E5; border: 1px solid #6366F1; color: white; "
            "padding: 2px 8px; font-size: 10px; font-weight: bold; border-radius: 5px; }"
            "QPushButton:hover { background-color: #6366F1; }"
        )
        play_btn.clicked.connect(lambda: os.startfile(clip_path) if sys.platform == "win32" else subprocess.Popen(["xdg-open", clip_path]))
        btn_row1.addWidget(play_btn, 1)

        self._copy_btn = QPushButton("📋 Copy Title")
        self._copy_btn.setFixedHeight(24)
        self._copy_btn.setToolTip("Copy Title + Hashtags to Clipboard")
        self._copy_btn.setStyleSheet(
            "QPushButton { background-color: #1E232E; border: 1px solid #333D4F; color: #E5E7EB; "
            "border-radius: 5px; font-size: 10px; font-weight: bold; padding: 2px 8px; }"
            "QPushButton:hover { background-color: #2B3444; border-color: #4F5D75; color: white; }"
        )
        self._copy_btn.clicked.connect(self._on_copy_title)
        btn_row1.addWidget(self._copy_btn, 1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(5)

        folder_btn = QPushButton("📁 Open Folder")
        folder_btn.setFixedHeight(24)
        folder_btn.setToolTip("Show video file highlighted in Windows File Explorer")
        folder_btn.setStyleSheet(
            "QPushButton { background-color: #1F242E; border: 1px solid #2E3646; color: #E5E7EB; "
            "border-radius: 5px; font-size: 10px; font-weight: 600; padding: 2px 6px; }"
            "QPushButton:hover { background-color: #28303E; border-color: #4B5563; }"
        )
        folder_btn.clicked.connect(self._on_open_folder)
        btn_row2.addWidget(folder_btn, 1)

        del_btn = QPushButton("🗑 Delete")
        del_btn.setFixedHeight(24)
        del_btn.setToolTip("Delete Clip, Metadata & Thumbnail from disk")
        del_btn.setStyleSheet(
            "QPushButton { background-color: #26161B; border: 1px solid #4C1D24; color: #F87171; "
            "border-radius: 5px; font-size: 10px; font-weight: bold; padding: 2px 6px; }"
            "QPushButton:hover { background-color: #EF4444; color: white; border-color: #EF4444; }"
        )
        del_btn.clicked.connect(self._on_delete)
        btn_row2.addWidget(del_btn, 1)

        btn_vbox.addLayout(btn_row1)
        btn_vbox.addLayout(btn_row2)
        info_layout.addLayout(btn_vbox)
        layout.addLayout(info_layout, 1)

    def _on_copy_title(self) -> None:
        """Copy English title + hashtags to Windows clipboard."""
        full_text = self.title_info.get("full_title", "")
        if not full_text:
            t = self.title_info.get("title", "")
            h = self.title_info.get("hashtags", "")
            full_text = f"{t} {h}".strip()
        QApplication.clipboard().setText(full_text)
        self._copy_btn.setText("Copied! ✓")
        self._copy_btn.setStyleSheet(
            "QPushButton { background-color: #065F46; border: 1px solid #10B981; color: #34D399; "
            "border-radius: 5px; font-size: 10px; font-weight: bold; padding: 2px 8px; }"
        )
        QTimer.singleShot(1800, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        try:
            self._copy_btn.setText("📋 Copy")
            self._copy_btn.setStyleSheet(
                "QPushButton { background-color: #1E232E; border: 1px solid #333D4F; color: #E5E7EB; "
                "border-radius: 5px; font-size: 10px; font-weight: bold; padding: 2px 8px; }"
                "QPushButton:hover { background-color: #2B3444; border-color: #4F5D75; color: white; }"
            )
        except RuntimeError:
            pass

    @Slot(str, QPixmap)
    def _on_thumbnail(self, path: str, pixmap: QPixmap) -> None:
        if path == self.clip_path and not pixmap.isNull():
            scaled = pixmap.scaled(
                self._thumb_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._thumb_label.setPixmap(scaled)

    def _on_open_folder(self) -> None:
        p = os.path.normpath(self.clip_path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{p}"], **get_subprocess_kwargs())
        else:
            folder = os.path.dirname(p)
            subprocess.Popen(["xdg-open", folder])

    def _on_delete(self) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Clip",
            f"Are you sure you want to delete this clip from disk?\n\n{Path(self.clip_path).name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            p = Path(self.clip_path)
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                p.with_suffix(".txt").unlink(missing_ok=True)
            except Exception:
                pass
            try:
                p.with_suffix(".jpg").unlink(missing_ok=True)
                p.with_suffix(".png").unlink(missing_ok=True)
            except Exception:
                pass
            self.clip_deleted.emit(self.clip_path)
            self.setParent(None)
            self.deleteLater()


# ===================================================================
# Hype Meter Widget (Creative Studio Gradient Bar)
# ===================================================================

class HypeMeter(QProgressBar):
    """Animated VU-meter style progress bar with modern gradient styling."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0, 100)
        self.setValue(0)
        self.setFixedHeight(22)
        self.setTextVisible(False)
        self._animation = QPropertyAnimation(self, b"value")
        self._animation.setDuration(350)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._update_color(0)

    def set_level(self, value: float) -> None:
        """Set the meter to a 0.0 – 1.0 value with smooth animation."""
        target = max(0, min(100, int(value * 100)))
        self._animation.stop()
        self._animation.setStartValue(self.value())
        self._animation.setEndValue(target)
        self._animation.start()
        self._update_color(target)

    def _update_color(self, pct: int) -> None:
        if pct >= 70:
            color = "#EF4444"
        elif pct >= 40:
            color = "#F59E0B"
        else:
            color = "#10B981"
        self.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: #11141A;
                border: 1px solid #242933;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 5px;
            }}
            """
        )


# ===================================================================
# Stat Card Widget (Telemetry Pill Card)
# ===================================================================

class StatCard(QFrame):
    """Small metric display card for telemetry."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setStyleSheet(
            """
            QFrame#statCard {
                background-color: #11141A;
                border: 1px solid #242933;
                border-radius: 8px;
            }
            """
        )
        self.setFixedHeight(68)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setStyleSheet(
            "color: #9CA3AF; font-size: 10px; font-weight: bold; letter-spacing: 0.6px;"
        )
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        self._value = QLabel("0")
        self._value.setStyleSheet(
            "color: #F3F4F6; font-size: 22px; font-weight: bold;"
        )
        self._value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._value)

    def set_value(self, v: int | str) -> None:
        self._value.setText(str(v))


# ===================================================================
# Main Window
# ===================================================================

class MainWindow(QMainWindow):
    """The primary application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1180, 680)
        self.resize(1340, 780)

        icon_path = get_app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        # --- Worker setup ---
        self._worker = PipelineWorker()
        self._worker.status_changed.connect(self._on_status)
        self._worker.log_message.connect(self._on_log)
        self._worker.segment_processed.connect(self._on_segment)
        self._worker.hype_detected.connect(self._on_hype)
        self._worker.hype_meter_update.connect(self._on_meter)
        self._worker.clip_ready.connect(self._on_clip_ready)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.progress_update.connect(self._on_progress)
        self._worker.finished.connect(self._on_worker_finished)

        # --- Log handler ---
        self._log_signal = _GuiLogSignal()
        self._log_signal.message.connect(self._on_log)
        handler = GuiLogHandler(self._log_signal)
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        # --- State ---
        self._running = False
        self._clip_count = 0
        self._hype_count = 0
        self._start_time: float | None = None

        # --- Settings & Output Directory ---
        settings = _load_settings()
        saved_folder = settings.get("output_folder")
        if (
            saved_folder
            and Path(saved_folder).is_dir()
            and "Program Files" not in str(saved_folder)
        ):
            self._output_dir = Path(saved_folder)
        else:
            self._output_dir = DEFAULT_OUTPUT_DIR
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            get_segments_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        # --- Build UI ---
        self._build_ui()
        icon_file = Path(BASE_DIR) / "assets" / "icon.ico"
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))
        self._setup_tray()
        self._setup_timer()
        self._refresh_gallery()

    # ------------------------------------------------------------------
    # UI Construction (3-Column Creative Studio Workstation)
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Top Application Header (64px)
        root_layout.addWidget(self._build_top_bar())

        # 2. Main 3-Column Creative Studio Workspace
        workspace = QHBoxLayout()
        workspace.setContentsMargins(14, 12, 14, 12)
        workspace.setSpacing(14)

        workspace.addWidget(self._build_left_column())
        workspace.addWidget(self._build_center_column(), 1)
        workspace.addWidget(self._build_right_column())

        root_layout.addLayout(workspace, 1)

        # 3. Status Bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("headerBar")
        bar.setFixedHeight(64)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(18, 0, 18, 0)
        bar_layout.setSpacing(16)

        # --- Left: Logo & Studio Pill ---
        left_box = QHBoxLayout()
        left_box.setSpacing(10)
        logo_icon = QLabel("⚡")
        logo_icon.setStyleSheet("font-size: 20px;")
        left_box.addWidget(logo_icon)

        title_lbl = QLabel("StreamClipper <span style='color:#6366F1;'>Pro</span>")
        title_lbl.setStyleSheet(
            "font-size: 16px; font-weight: 800; color: #F3F4F6; letter-spacing: -0.3px;"
        )
        left_box.addWidget(title_lbl)

        pill_badge = QLabel("AI STUDIO")
        pill_badge.setStyleSheet(
            "background-color: #231F3D; color: #A5B4FC; border: 1px solid #4338CA; "
            "border-radius: 9px; padding: 2px 8px; font-size: 10px; font-weight: 800; letter-spacing: 0.6px;"
        )
        left_box.addWidget(pill_badge)
        bar_layout.addLayout(left_box)

        bar_layout.addSpacing(6)

        # --- Center: Wide URL Input + Browse Video ---
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("🔗  Enter YouTube Live / Video URL or select a local video file…")
        self._url_input.setFixedHeight(40)
        self._url_input.setMinimumWidth(380)
        self._url_input.textChanged.connect(self._on_input_text_changed)
        bar_layout.addWidget(self._url_input, 1)

        self._browse_vid_btn = QPushButton("📁 Browse Video")
        self._browse_vid_btn.setFixedHeight(40)
        self._browse_vid_btn.setToolTip("Open a local .mp4 / .mkv / .mov video file from your computer")
        self._browse_vid_btn.setStyleSheet(
            "QPushButton { background-color: #1A1F29; border: 1px solid #2B3342; color: #E5E7EB; "
            "font-size: 12px; font-weight: 600; padding: 0 14px; border-radius: 8px; } "
            "QPushButton:hover { background-color: #242B38; border-color: #6366F1; color: #FFFFFF; }"
        )
        self._browse_vid_btn.clicked.connect(self._on_browse_video_file)
        bar_layout.addWidget(self._browse_vid_btn)

        # --- Right: Action CTAs ---
        self._vod_btn = QPushButton("📼 Process VOD")
        self._vod_btn.setFixedHeight(40)
        self._vod_btn.setToolTip("Force full video analysis and highlight extraction")
        self._vod_btn.setStyleSheet(
            "QPushButton { background-color: #1A1F29; border: 1px solid #2B3342; color: #E5E7EB; "
            "font-size: 12px; font-weight: 600; padding: 0 16px; border-radius: 8px; } "
            "QPushButton:hover { background-color: #242B38; border-color: #4B5563; }"
        )
        self._vod_btn.clicked.connect(self._on_start_vod)
        bar_layout.addWidget(self._vod_btn)

        self._start_btn = QPushButton("▶  START MONITORING")
        self._start_btn.setFixedHeight(40)
        self._start_btn.setMinimumWidth(190)
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #10B981; border: 1px solid #34D399; color: #FFFFFF; "
            "font-size: 13px; font-weight: 800; padding: 0 18px; border-radius: 8px; letter-spacing: 0.3px; } "
            "QPushButton:hover { background-color: #059669; }"
        )
        self._start_btn.clicked.connect(self._on_primary_action)
        bar_layout.addWidget(self._start_btn)

        return bar

    def _build_left_column(self) -> QWidget:
        col = QWidget()
        col.setFixedWidth(290)
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        c_layout = QVBoxLayout(container)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(12)

        # --- Card 1: Video & Format ---
        card1, c1 = self._make_card("VIDEO & FORMAT")

        c1.addWidget(self._make_label("Aspect Ratio"))
        self._aspect_combo = QComboBox()
        self._aspect_combo.addItems(list(ASPECT_RATIOS.keys()))
        self._aspect_combo.setToolTip("Target Video Aspect Ratio")
        self._aspect_combo.currentTextChanged.connect(self._on_aspect_changed)
        c1.addWidget(self._aspect_combo)

        c1.addWidget(self._make_label("Clip Duration"))
        self._dur_combo = QComboBox()
        self._dur_combo.addItems(["60 seconds", "45 seconds", "30 seconds", "15 seconds"])
        self._dur_combo.setToolTip("Highlight Clip Duration")
        c1.addWidget(self._dur_combo)

        c1.addWidget(self._make_label("Clip Interval"))
        self._interval_combo = QComboBox()
        self._interval_combo.addItems([
            "Every 10s (Rapid Sliding Window)",
            "Every 30s",
            "Hype Spike Only",
        ])
        self._interval_combo.setToolTip("Sliding window capture interval (keeps full 60s duration)")
        c1.addWidget(self._interval_combo)

        c1.addWidget(self._make_label("Export Quality"))
        self._quality_combo = QComboBox()
        self._quality_combo.addItems(list(QUALITY_PROFILES.keys()))
        self._quality_combo.setToolTip("Export Video Resolution and Bitrate")
        c1.addWidget(self._quality_combo)

        c_layout.addWidget(card1)

        # --- Card 2: AI & Captions ---
        card2, c2 = self._make_card("AI & CAPTIONS")

        c2.addWidget(self._make_label("Caption Style"))
        self._style_combo = QComboBox()
        self._style_combo.addItems(list(CAPTION_STYLES.keys()))
        self._style_combo.setToolTip("Burned-in Animated ASS Caption Preset")
        c2.addWidget(self._style_combo)

        c2.addWidget(self._make_label("Transcription Accuracy"))
        self._accuracy_combo = QComboBox()
        self._accuracy_combo.addItems([
            "Balanced (Whisper Small - Fast & Accurate)",
            "High Accuracy (Whisper Medium - Best Offline)",
            "Ultra AI (Gemini 2.5 Flash - Uses API Key)",
        ])
        self._accuracy_combo.setToolTip("Whisper model size & Gemini 2.5 Flash Ultra AI transcript refinement")
        c2.addWidget(self._accuracy_combo)

        c2.addWidget(self._make_label("Speech Language"))
        self._lang_combo = QComboBox()
        self._lang_combo.addItems(list(LANGUAGES.keys()))
        self._lang_combo.setToolTip("Speech recognition audio language")
        c2.addWidget(self._lang_combo)

        c2.addSpacing(4)
        self._face_tracking_check = QCheckBox("Smart Face Tracking")
        self._face_tracking_check.setChecked(True)
        self._face_tracking_check.setToolTip("Auto-detect and smoothly center on the active speaker for vertical/square crops")
        c2.addWidget(self._face_tracking_check)

        hint = QLabel("Auto-centers speaker with cinematic pan")
        hint.setStyleSheet("color: #6B7280; font-size: 10px; margin-left: 24px;")
        c2.addWidget(hint)

        c2.addSpacing(6)
        c2.addWidget(self._make_label("Gemini API Key (Optional)"))
        self._gemini_api_key_input = QLineEdit()
        self._gemini_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._gemini_api_key_input.setPlaceholderText("Paste API Key for AI Titles…")
        self._gemini_api_key_input.setFixedHeight(30)
        saved_key = (_load_settings().get("gemini_api_key") or "").strip() or DEFAULT_GEMINI_API_KEY
        self._gemini_api_key_input.setText(saved_key)
        self._gemini_api_key_input.textChanged.connect(self._on_api_key_changed)
        c2.addWidget(self._gemini_api_key_input)

        api_hint = QLabel("AI converts any language to viral English hooks")
        api_hint.setStyleSheet("color: #6B7280; font-size: 10px; margin-left: 2px;")
        c2.addWidget(api_hint)

        c_layout.addWidget(card2)

        # --- Card 3: Export Destination ---
        card3, c3 = self._make_card("OUTPUT DESTINATION")

        c3.addWidget(self._make_label("Save Folder"))
        self._output_path_display = QLineEdit(str(self._output_dir))
        self._output_path_display.setReadOnly(True)
        self._output_path_display.setToolTip(str(self._output_dir))
        self._output_path_display.setFixedHeight(32)
        c3.addWidget(self._output_path_display)

        dest_btns = QHBoxLayout()
        dest_btns.setSpacing(8)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.setFixedHeight(30)
        self._browse_btn.clicked.connect(self._on_browse_output)
        dest_btns.addWidget(self._browse_btn, 1)

        self._open_btn = QPushButton("Open")
        self._open_btn.setFixedHeight(30)
        self._open_btn.clicked.connect(self._on_open_output)
        dest_btns.addWidget(self._open_btn, 1)
        c3.addLayout(dest_btns)

        c_layout.addWidget(card3)
        c_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)
        return col

    def _build_center_column(self) -> QWidget:
        col = QWidget()
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # --- Top Card: Engine Telemetry ---
        t_card, t_layout = self._make_card("")
        t_layout.setSpacing(12)

        # Status badge row
        status_row = QHBoxLayout()
        self._status_label = QLabel("🔘  Engine Idle")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setFixedHeight(38)
        self._status_label.setStyleSheet(
            "QLabel { background-color: #181C24; border: 1px solid #242933; "
            "border-radius: 19px; font-size: 13px; font-weight: bold; padding: 6px 18px; color: #9CA3AF; }"
        )
        status_row.addWidget(self._status_label, 1)
        t_layout.addLayout(status_row)

        # Metrics row
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(10)
        self._stat_segments = StatCard("SEGMENTS BUFFERED")
        self._stat_hype = StatCard("HYPE DETECTIONS")
        self._stat_clips = StatCard("CLIPS RENDERED")
        metrics_row.addWidget(self._stat_segments)
        metrics_row.addWidget(self._stat_hype)
        metrics_row.addWidget(self._stat_clips)
        t_layout.addLayout(metrics_row)

        # Hype Meter section
        meter_header = QHBoxLayout()
        meter_title = QLabel("REAL-TIME HYPE INTENSITY")
        meter_title.setStyleSheet("color: #9CA3AF; font-size: 10px; font-weight: 800; letter-spacing: 0.8px;")
        meter_header.addWidget(meter_title)
        meter_header.addStretch()

        self._hype_pct_label = QLabel("0%")
        self._hype_pct_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 800;")
        meter_header.addWidget(self._hype_pct_label)
        t_layout.addLayout(meter_header)

        self._hype_meter = HypeMeter()
        t_layout.addWidget(self._hype_meter)

        layout.addWidget(t_card)

        # --- Bottom Card: Activity Console ---
        c_card, c_layout = self._make_card("")
        c_layout.setSpacing(8)

        log_hdr = QHBoxLayout()
        log_title = QLabel("TERMINAL ACTIVITY LOG")
        log_title.setStyleSheet("color: #9CA3AF; font-size: 10px; font-weight: 800; letter-spacing: 0.8px;")
        log_hdr.addWidget(log_title)
        log_hdr.addStretch()

        self._clear_log_btn = QPushButton("Clear Log")
        self._clear_log_btn.setFixedHeight(24)
        self._clear_log_btn.setStyleSheet(
            "QPushButton { background-color: #181C24; border: 1px solid #28303E; color: #9CA3AF; "
            "font-size: 11px; font-weight: 600; padding: 0 10px; border-radius: 5px; } "
            "QPushButton:hover { background-color: #222733; color: #F3F4F6; }"
        )
        self._clear_log_btn.clicked.connect(self._log_console_clear)
        log_hdr.addWidget(self._clear_log_btn)
        c_layout.addLayout(log_hdr)

        self._log_console = QTextEdit()
        self._log_console.setReadOnly(True)
        self._log_console.setFont(QFont("Consolas", 10))
        c_layout.addWidget(self._log_console, 1)

        layout.addWidget(c_card, 1)
        return col

    def _build_right_column(self) -> QWidget:
        col = QWidget()
        col.setFixedWidth(340)
        layout = QVBoxLayout(col)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        g_card, g_layout = self._make_card("")
        g_layout.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("GENERATED CLIPS")
        title.setStyleSheet("color: #9CA3AF; font-size: 10px; font-weight: 800; letter-spacing: 0.8px;")
        hdr.addWidget(title)
        hdr.addStretch()

        self._clip_count_badge = QLabel("0 CLIPS")
        self._clip_count_badge.setStyleSheet(
            "QLabel { background-color: #231F3D; color: #A5B4FC; border: 1px solid #4338CA; "
            "border-radius: 9px; padding: 2px 8px; font-weight: 800; font-size: 10px; letter-spacing: 0.4px; }"
        )
        hdr.addWidget(self._clip_count_badge)
        g_layout.addLayout(hdr)

        # Scroll area for clip cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._gallery_container = QWidget()
        self._gallery_layout = QVBoxLayout(self._gallery_container)
        self._gallery_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._gallery_layout.setSpacing(10)
        self._gallery_layout.setContentsMargins(0, 4, 0, 4)

        # Placeholder
        self._gallery_placeholder = QLabel(
            "<div style='text-align:center; color:#4B5563; padding:50px 16px;'>"
            "<p style='font-size:36px; margin-bottom:8px;'>📂</p>"
            "<p style='font-size:13px; font-weight:bold; color:#9CA3AF;'>No clips yet</p>"
            "<p style='font-size:11px; color:#6B7280;'>Rendered vertical clips will appear here</p>"
            "</div>"
        )
        self._gallery_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gallery_layout.addWidget(self._gallery_placeholder)

        scroll.setWidget(self._gallery_container)
        g_layout.addWidget(scroll, 1)

        layout.addWidget(g_card, 1)
        return col

    @staticmethod
    def _make_card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("panelCard")
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(14, 12, 14, 12)
        vbox.setSpacing(8)
        if title:
            hdr = QLabel(title)
            hdr.setStyleSheet(
                "color: #9CA3AF; font-size: 10px; font-weight: 800; letter-spacing: 0.8px;"
            )
            vbox.addWidget(hdr)
        return card, vbox

    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #9CA3AF; font-size: 11px; font-weight: 600; margin-top: 2px;")
        return lbl

    def _log_console_clear(self) -> None:
        self._log_console.clear()

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        icon_path = get_app_icon_path()
        if icon_path:
            self._tray.setIcon(QIcon(str(icon_path)))
        else:
            pm = QPixmap(32, 32)
            pm.fill(QColor("#0099ff"))
            self._tray.setIcon(QIcon(pm))
        self._tray.setToolTip(APP_NAME)
        self._tray.show()

    def _setup_timer(self) -> None:
        """Elapsed time ticker for the status bar."""
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

    # ------------------------------------------------------------------
    # Configuration Builder
    # ------------------------------------------------------------------

    def _build_config(self) -> PipelineConfig:
        dur_txt = self._dur_combo.currentText()
        if "15" in dur_txt:
            clip_dur = 15
        elif "30" in dur_txt:
            clip_dur = 30
        elif "45" in dur_txt:
            clip_dur = 45
        else:
            clip_dur = 60

        interval_txt = (
            self._interval_combo.currentText()
            if hasattr(self, "_interval_combo")
            else "Every 10s"
        )
        if "10s" in interval_txt:
            clip_interval_sec = 10
        elif "30s" in interval_txt:
            clip_interval_sec = 30
        else:
            clip_interval_sec = 0

        style_name = self._style_combo.currentText()
        style_overrides = CAPTION_STYLES.get(style_name, {})
        lang_name = self._lang_combo.currentText()
        language = LANGUAGES.get(lang_name)
        aspect_name = self._aspect_combo.currentText()
        aspect_info = ASPECT_RATIOS.get(aspect_name, ASPECT_RATIOS["9:16 (Shorts / Reels)"])
        quality_name = self._quality_combo.currentText()
        quality_info = QUALITY_PROFILES.get(quality_name, QUALITY_PROFILES["1080p Ultra (High Bitrate)"])

        if getattr(sys, "frozen", False):
            app_dir = Path(sys.executable).resolve().parent
        else:
            app_dir = Path(__file__).resolve().parent

        # Compute dimensions based on aspect ratio and quality preset
        scale_factor = quality_info.get("scale_factor", 1.0)
        out_w = int(aspect_info["width"] * scale_factor)
        out_h = int(aspect_info["height"] * scale_factor)
        out_w = out_w - (out_w % 2)
        out_h = out_h - (out_h % 2)

        editor_kwargs: dict[str, Any] = {
            "output_dir": self._output_dir,
            "aspect_ratio_name": aspect_name,
            "output_width": out_w,
            "output_height": out_h,
            "margin_v": max(40, int(aspect_info["margin_v"] * scale_factor)),
            "quality": quality_name,
            "crf": quality_info["crf"],
            "preset": quality_info["preset"],
            "audio_bitrate": quality_info["audio_bitrate"],
            "enable_face_tracking": self._face_tracking_check.isChecked() and "16:9" not in aspect_name,
        }
        for k, v in style_overrides.items():
            if k == "margin_v":
                continue  # Let aspect ratio control vertical margin
            editor_kwargs[k] = v

        base_font_size = int(editor_kwargs.get("font_size", 78))
        editor_kwargs["font_size"] = max(24, int(base_font_size * aspect_info.get("font_scale", 1.0) * scale_factor))

        accuracy_mode = (
            self._accuracy_combo.currentText()
            if hasattr(self, "_accuracy_combo")
            else "Balanced (Whisper Small - Fast & Accurate)"
        )
        model_size = "medium" if "Medium" in accuracy_mode else "small"
        gemini_key = (
            self._gemini_api_key_input.text().strip()
            if hasattr(self, "_gemini_api_key_input")
            else ""
        ) or DEFAULT_GEMINI_API_KEY

        config = PipelineConfig(
            ingestor=IngestorConfig(
                segment_duration=10 if clip_interval_sec > 0 else clip_dur,
                output_dir=get_segments_dir(),
                format="ts",
            ),
            hype=HypeConfig(
                clip_duration=clip_dur,
                clip_interval_seconds=clip_interval_sec,
                clip_interval_name=interval_txt,
            ),
            transcriber=TranscriberConfig(
                model_size=model_size,
                accuracy_mode=accuracy_mode,
                language=language,
                gemini_api_key=gemini_key,
                stream_title=self._url_input.text().strip() if hasattr(self, "_url_input") else "",
            ),
            editor=EditorConfig(**editor_kwargs),
            gemini_api_key=gemini_key,
        )
        return config

    def _on_api_key_changed(self, text: str) -> None:
        _save_settings({"gemini_api_key": text.strip()})

    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_aspect_changed(self, text: str) -> None:
        """Handle aspect ratio dropdown change."""
        if "16:9" in text:
            self._face_tracking_check.setEnabled(False)
            self._face_tracking_check.setToolTip("Face tracking is not required for full 16:9 landscape mode")
        else:
            self._face_tracking_check.setEnabled(True)
            self._face_tracking_check.setToolTip(
                "Auto-detect and smoothly center on the active speaker's face for vertical/square crops"
            )

    @Slot()
    def _on_browse_output(self) -> None:
        """Open file dialog to select a custom output folder."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", str(self._output_dir)
        )
        if chosen:
            self._output_dir = Path(chosen)
            self._output_path_display.setText(str(self._output_dir))
            self._output_path_display.setToolTip(str(self._output_dir))
            _save_settings({"output_folder": str(self._output_dir)})
            self._on_log(f"📁 Output folder updated to: {self._output_dir}", "INFO")
            self._refresh_gallery()

    @Slot()
    def _on_open_output(self) -> None:
        """Open the currently selected output folder in Windows File Explorer."""
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                ["explorer", os.path.normpath(str(self._output_dir))],
                **get_subprocess_kwargs(),
            )
        except Exception as exc:
            self._on_log(f"Could not open output folder: {exc}", "ERROR")

    def _refresh_gallery(self) -> None:
        """Scan current output directory and populate gallery cards."""
        # Clear existing cards except placeholder
        for i in reversed(range(self._gallery_layout.count())):
            item = self._gallery_layout.itemAt(i)
            widget = item.widget() if item else None
            if widget and widget is not self._gallery_placeholder:
                widget.setParent(None)
                widget.deleteLater()

        if not self._output_dir.is_dir():
            self._gallery_placeholder.show()
            self._clip_count = 0
            self._clip_count_badge.setText("0")
            return

        mp4_files = sorted(
            self._output_dir.glob("*.mp4"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        self._clip_count = len(mp4_files)
        self._clip_count_badge.setText(f"{self._clip_count} CLIPS" if self._clip_count != 1 else "1 CLIP")

        if mp4_files:
            self._gallery_placeholder.hide()
            for mp4 in mp4_files[:30]:
                card = ClipCard(str(mp4), 0.0, 0.0, 1.0, parent=self._gallery_container)
                card.clip_deleted.connect(self._on_clip_deleted)
                self._gallery_layout.addWidget(card)
        else:
            self._gallery_placeholder.show()

    @Slot()
    def _on_browse_video_file(self) -> None:
        """Open file dialog to select a local video file."""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Select Local Video File",
            str(Path.home() / "Videos"),
            "Video Files (*.mp4 *.mkv *.mov *.webm *.avi *.flv *.ts *.m4v *.wmv);;All Files (*.*)",
        )
        if chosen:
            self._url_input.setText(chosen)
            self._on_log(f"🎬 Local video selected: {chosen}", "INFO")

    def _on_input_text_changed(self, text: str) -> None:
        """Update CTA button label dynamically based on input."""
        if self._running:
            return
        cleaned = text.strip().strip('"').strip("'")
        if not cleaned:
            self._start_btn.setText("▶  START MONITORING")
            return
        p = Path(cleaned)
        if p.is_file() or any(cleaned.lower().endswith(ext) for ext in SUPPORTED_VIDEO_EXTENSIONS):
            self._start_btn.setText("⚡  PROCESS VIDEO")
        elif "youtube.com" in cleaned or "youtu.be" in cleaned:
            if "/live" in cleaned.lower() or "stream" in cleaned.lower():
                self._start_btn.setText("▶  START MONITORING")
            else:
                self._start_btn.setText("⚡  PROCESS YOUTUBE")
        else:
            self._start_btn.setText("⚡  START PROCESSING")

    @Slot()
    def _on_primary_action(self) -> None:
        if self._running:
            self._stop_pipeline()
            return

        raw = self._url_input.text().strip()
        if not raw:
            QMessageBox.warning(self, "Missing Input", "Please enter a YouTube URL or select a local video file.")
            return

        self._on_log(f"🔍 Inspecting input: {raw}", "INFO")
        self._on_status("analyzing")
        self._status_bar.showMessage("Detecting input source...")
        QApplication.processEvents()

        source_info = detect_input_source(raw)
        src_type = source_info.get("type", "unknown")
        title = source_info.get("title", "")

        config = self._build_config()

        if src_type == "local_file":
            local_path = str(source_info["path"])
            dur = source_info.get("duration", 0.0)
            dur_str = f" ({_fmt_time(dur)})" if dur > 0 else ""
            self._on_log(f"🎬 Processing local video: '{title}'{dur_str}", "INFO")
            self._start_pipeline("vod", local_path, config)
        elif src_type == "youtube_vod":
            dur = source_info.get("duration", 0.0)
            dur_str = f" ({_fmt_time(dur)})" if dur > 0 else ""
            self._on_log(f"📼 Processing YouTube VOD: '{title}'{dur_str}", "INFO")
            self._start_pipeline("vod", source_info["url"], config)
        elif src_type == "youtube_live":
            self._on_log(f"🔴 Starting live stream monitoring: '{title}'", "INFO")
            self._start_pipeline("live", source_info["url"], config)
        else:
            self._on_log(f"⚠️ Unrecognized input type; attempting stream ingest for: {raw}", "WARNING")
            self._start_pipeline("live", raw, config)

    @Slot()
    def _on_start_live(self) -> None:
        if self._running:
            self._stop_pipeline()
            return

        url = self._url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a YouTube URL.")
            return

        config = self._build_config()
        self._start_pipeline("live", url, config)

    @Slot()
    def _on_start_vod(self) -> None:
        if self._running:
            QMessageBox.information(
                self, "Busy", "A pipeline is already running. Stop it first.",
            )
            return

        url = self._url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a YouTube URL or select a video file.")
            return

        config = self._build_config()
        self._start_pipeline("vod", url, config)

    def _start_pipeline(self, mode: str, url: str, config: PipelineConfig) -> None:
        self._running = True
        self._clip_count = 0
        self._hype_count = 0
        self._start_time = time.time()

        self._stat_segments.set_value(0)
        self._stat_hype.set_value(0)
        self._stat_clips.set_value(0)

        # Toggle button to STOP
        self._start_btn.setText("⏹  STOP MONITORING")
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #EF4444; border: 1px solid #F87171; color: #FFFFFF; "
            "font-size: 13px; font-weight: 800; padding: 0 18px; border-radius: 8px; letter-spacing: 0.3px; } "
            "QPushButton:hover { background-color: #DC2626; }"
        )
        self._browse_vid_btn.setEnabled(False)
        self._vod_btn.setEnabled(False)
        self._url_input.setEnabled(False)
        self._dur_combo.setEnabled(False)
        self._interval_combo.setEnabled(False)
        self._aspect_combo.setEnabled(False)
        self._quality_combo.setEnabled(False)
        self._accuracy_combo.setEnabled(False)
        self._style_combo.setEnabled(False)
        self._lang_combo.setEnabled(False)
        self._face_tracking_check.setEnabled(False)
        self._browse_btn.setEnabled(False)

        self._elapsed_timer.start()
        self._worker.configure(mode, url, config)
        self._worker.start()

    def _stop_pipeline(self) -> None:
        self._on_log("Stop requested…", "WARNING")
        self._worker.request_stop()
        self._start_btn.setEnabled(False)
        self._start_btn.setText("⏳ Stopping…")

    def _reset_ui(self) -> None:
        self._running = False
        self._elapsed_timer.stop()
        self._on_input_text_changed(self._url_input.text())
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #10B981; border: 1px solid #34D399; color: #FFFFFF; "
            "font-size: 13px; font-weight: 800; padding: 0 18px; border-radius: 8px; letter-spacing: 0.3px; } "
            "QPushButton:hover { background-color: #059669; }"
        )
        self._start_btn.setEnabled(True)
        self._browse_vid_btn.setEnabled(True)
        self._vod_btn.setEnabled(True)
        self._url_input.setEnabled(True)
        self._dur_combo.setEnabled(True)
        self._interval_combo.setEnabled(True)
        self._aspect_combo.setEnabled(True)
        self._quality_combo.setEnabled(True)
        self._accuracy_combo.setEnabled(True)
        self._style_combo.setEnabled(True)
        self._lang_combo.setEnabled(True)
        self._face_tracking_check.setEnabled("16:9" not in self._aspect_combo.currentText())
        self._browse_btn.setEnabled(True)
        self._status_bar.showMessage("Ready")

    # ------------------------------------------------------------------
    # Signal Slots
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_status(self, status: str) -> None:
        badge_map = {
            "idle":           ("🔘  Engine Idle",            "#181C24", "#242933", "#9CA3AF"),
            "buffering":      ("🔄  Buffering Live Stream…",  "#162138", "#2563EB", "#60A5FA"),
            "downloading":    ("📥  Downloading Video…",      "#1E2A38", "#0284C7", "#38BDF8"),
            "analyzing":      ("🔍  Analyzing Telemetry…",    "#162138", "#2563EB", "#60A5FA"),
            "hype_detected":  ("🔥  Hype Peak Detected!",   "#2F1A17", "#EA580C", "#FB923C"),
            "transcribing":   ("🎙  Whisper Transcribing…",  "#261A38", "#9333EA", "#C084FC"),
            "rendering":      ("🎬  FFmpeg Rendering Clip…", "#2B172E", "#DB2777", "#F472B6"),
            "clip_ready":     ("✅  Clip Rendered!",          "#12281E", "#059669", "#34D399"),
            "completed":      ("🎉  Processing Complete!",    "#12281E", "#059669", "#34D399"),
        }
        text, bg, border, fg = badge_map.get(status, ("🔘  Engine Idle", "#181C24", "#242933", "#9CA3AF"))
        self._status_label.setText(text)
        self._status_label.setStyleSheet(
            f"QLabel {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 19px; font-size: 13px; font-weight: bold; padding: 6px 18px; color: {fg}; }}"
        )

    @Slot(str, str)
    def _on_log(self, message: str, level: str) -> None:
        color = LOG_COLOURS.get(level, "#c8c8c8")
        html = f"<span style='color:{color};'>{message}</span>"
        self._log_console.append(html)

        # Trim to 500 lines
        doc = self._log_console.document()
        if doc.blockCount() > 500:
            cursor = self._log_console.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(
                cursor.MoveOperation.Down,
                cursor.MoveMode.KeepAnchor,
                doc.blockCount() - 500,
            )
            cursor.removeSelectedText()

    @Slot(int)
    def _on_segment(self, count: int) -> None:
        self._stat_segments.set_value(count)

    @Slot(float, float)
    def _on_hype(self, timestamp: float, score: float) -> None:
        self._hype_count += 1
        self._stat_hype.set_value(self._hype_count)

    @Slot(float)
    def _on_meter(self, value: float) -> None:
        self._hype_meter.set_level(value)
        pct = int(max(0.0, min(1.0, value)) * 100)
        self._hype_pct_label.setText(f"{pct}%")
        if pct >= 70:
            self._hype_pct_label.setStyleSheet("color: #EF4444; font-size: 12px; font-weight: 800;")
        elif pct >= 40:
            self._hype_pct_label.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: 800;")
        else:
            self._hype_pct_label.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 800;")

    @Slot(str, float, float, float, dict)
    def _on_clip_ready(
        self,
        path: str,
        start: float,
        end: float,
        score: float,
        title_info: dict | None = None,
    ) -> None:
        self._clip_count += 1
        self._stat_clips.set_value(self._clip_count)
        self._clip_count_badge.setText(f"{self._clip_count} CLIPS" if self._clip_count != 1 else "1 CLIP")

        # Remove placeholder if present
        if self._gallery_placeholder.isVisible():
            self._gallery_placeholder.hide()

        # Add card at the top
        card = ClipCard(path, start, end, score, title_info=title_info, parent=self._gallery_container)
        card.clip_deleted.connect(self._on_clip_deleted)
        self._gallery_layout.insertWidget(0, card)

        # Tray notification
        if self._tray.isVisible():
            title_text = title_info.get("title", "") if title_info else Path(path).name
            self._tray.showMessage(
                "🎬 New Clip Ready",
                f"{title_text}  (score {score:.0%})",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    @Slot(str)
    def _on_clip_deleted(self, clip_path: str) -> None:
        self._clip_count = max(0, self._clip_count - 1)
        self._stat_clips.set_value(self._clip_count)
        self._clip_count_badge.setText(
            f"{self._clip_count} CLIPS" if self._clip_count != 1 else "1 CLIP"
        )
        if self._clip_count == 0:
            self._gallery_placeholder.show()
        self._on_log(f"Deleted clip: {Path(clip_path).name}", "INFO")

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self._on_log(f"ERROR: {msg}", "ERROR")
        QMessageBox.critical(self, "Pipeline Error", msg)

    @Slot(int, int)
    def _on_progress(self, current: int, total: int) -> None:
        self._status_bar.showMessage(
            f"Segmenting: {current}/{total}"
        )

    @Slot()
    def _on_worker_finished(self) -> None:
        self._reset_ui()
        self._on_status("idle")

    @Slot()
    def _tick_elapsed(self) -> None:
        if self._start_time:
            elapsed = int(time.time() - self._start_time)
            m, s = divmod(elapsed, 60)
            h, m = divmod(m, 60)
            self._status_bar.showMessage(f"Running — {h:02d}:{m:02d}:{s:02d}")

    # ------------------------------------------------------------------
    # Window Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._running:
            reply = QMessageBox.question(
                self,
                "Quit?",
                "A pipeline is still running. Stop and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            self._worker.request_stop()
            self._worker.wait(5000)

        self._tray.hide()
        event.accept()


# ===================================================================
# Helpers
# ===================================================================

def _extract_video_id(url: str) -> str | None:
    import re
    for pattern in [
        r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _get_duration(path: Path) -> float:
    return get_video_duration(path)


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ===================================================================
# Entry Point
# ===================================================================

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Devan920.StreamClipperPro.1.0")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)

    icon_path = get_app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
