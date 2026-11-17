"""VOD and Local Video Processor for StreamClipper Pro.

Provides:
- Auto-detection between Local Video Files, YouTube Live Streams, and YouTube VODs.
- Clean yt-dlp downloading for YouTube VODs with suppressed terminal windows.
- Ultra-fast full-track audio RMS energy & peak detection using numpy to find
  the top 3 to 5 highest-energy non-overlapping highlight moments.
- Seamless rendering with face tracking, ASS caption presets, and viral titles.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Supported local video file extensions
SUPPORTED_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v", ".wmv",
})


def get_subprocess_kwargs() -> Dict[str, Any]:
    """Return creationflags and startupinfo to completely suppress terminal pop-ups on Windows."""
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _resolve_binary(name: str) -> str:
    """Resolve executable from PATH or known locations."""
    found = shutil.which(name)
    if found:
        return found
    base_dir = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    candidates = [
        Path(base_dir) / f"{name}.exe",
        Path(sys.executable).parent / f"{name}.exe",
        Path(os.path.expandvars(r"%USERPROFILE%\.spotiflac")) / f"{name}.exe",
        Path(r"C:\Program Files\Topaz Labs LLC\Topaz Video AI") / f"{name}.exe",
        Path(__file__).resolve().parent / "dist" / "StreamClipperPro" / f"{name}.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return name


def get_video_duration(video_path: Path) -> float:
    """Extract total video duration in seconds via ffprobe."""
    ffprobe_bin = _resolve_binary("ffprobe")
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(Path(video_path).resolve()),
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            **get_subprocess_kwargs(),
        )
        val = res.stdout.strip()
        return float(val) if val else 0.0
    except Exception as exc:
        logger.warning("ffprobe duration check failed for %s: %s", video_path, exc)
        return 0.0


def detect_input_source(input_str: str) -> Dict[str, Any]:
    """Inspect input string and classify as local file, active live stream, or VOD.

    Returns
    -------
    dict:
        ``type``: 'local_file' | 'youtube_live' | 'youtube_vod' | 'unknown'
        ``path``: Path or None
        ``url``: str
        ``title``: str
        ``duration``: float
        ``is_live``: bool
    """
    raw = (input_str or "").strip().strip('"').strip("'")
    if not raw:
        return {"type": "unknown", "path": None, "url": "", "title": "", "duration": 0.0, "is_live": False}

    # 1. Local file check
    p = Path(raw)
    if p.is_file():
        dur = get_video_duration(p)
        return {
            "type": "local_file",
            "path": p.resolve(),
            "url": str(p.resolve()),
            "title": p.stem.replace("_", " ").title(),
            "duration": dur,
            "is_live": False,
        }

    # 2. YouTube / Web URL check
    if raw.startswith(("http://", "https://", "www.", "youtube.com", "youtu.be")):
        try:
            import yt_dlp
            ydl_opts = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(raw, download=False)
                if info:
                    is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
                    title = info.get("title", "YouTube Video")
                    dur = float(info.get("duration") or 0.0)
                    return {
                        "type": "youtube_live" if is_live else "youtube_vod",
                        "path": None,
                        "url": raw,
                        "title": title,
                        "duration": dur,
                        "is_live": is_live,
                    }
        except Exception as exc:
            logger.warning("yt-dlp metadata probe failed for %s: %s", raw, exc)

        # Fallback heuristic: /live or /stream in URL implies live
        is_live_url = "/live" in raw.lower() or "stream" in raw.lower()
        return {
            "type": "youtube_live" if is_live_url else "youtube_vod",
            "path": None,
            "url": raw,
            "title": "YouTube Video",
            "duration": 0.0,
            "is_live": is_live_url,
        }

    return {"type": "unknown", "path": None, "url": raw, "title": "", "duration": 0.0, "is_live": False}


def download_youtube_vod(
    url: str,
    output_dir: Path,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Tuple[Path, str]:
    """Download a normal YouTube video (VOD) up to 1080p using yt-dlp without terminal flashes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(output_dir / "vod_%(id)s.%(ext)s")
    video_title = "YouTube Video"

    if log_callback:
        log_callback(f"Downloading YouTube video: {url}", "INFO")

    try:
        import yt_dlp
        ydl_opts = {
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best[ext=mp4]/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                video_title = info.get("title", video_title)
    except Exception as exc:
        if log_callback:
            log_callback(f"Direct yt-dlp error ({exc}); trying CLI fallback...", "WARNING")
        # CLI fallback with CREATE_NO_WINDOW
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best[ext=mp4]/best",
            "--extractor-args", "youtube:player_client=android,web",
            "-o", out_template,
            "--merge-output-format", "mp4",
            "-q", url,
        ]
        subprocess.run(cmd, check=True, capture_output=True, **get_subprocess_kwargs())

    # Find the downloaded mp4 file
    candidates = list(output_dir.glob("vod_*.mp4"))
    if not candidates:
        candidates = list(output_dir.glob("vod_*.*"))
    if not candidates:
        raise RuntimeError(f"Download failed — no file found in {output_dir}")

    downloaded_path = candidates[0].resolve()
    return downloaded_path, video_title


def scan_audio_peaks(
    video_path: Path,
    clip_duration: float = 30.0,
    top_k: int = 4,
    window_sec: float = 0.5,
    sample_rate: int = 16000,
) -> List[Tuple[float, float, float]]:
    """Analyze full audio track energy via ffmpeg PCM pipe to locate top non-overlapping moments.

    Parameters
    ----------
    video_path:
        Path to local video file.
    clip_duration:
        Target duration per highlight clip (e.g., 30s or 60s).
    top_k:
        Number of highlight moments to return (3 to 5).
    window_sec:
        Energy aggregation window in seconds.
    sample_rate:
        Audio sampling rate for energy computation.

    Returns
    -------
    list of (clip_start, clip_end, score)
    """
    video_path = Path(video_path).resolve()
    total_duration = get_video_duration(video_path)
    if total_duration <= 0:
        total_duration = 60.0

    # Ensure clip duration does not exceed video duration
    clip_duration = min(clip_duration, total_duration)

    ffmpeg_bin = _resolve_binary("ffmpeg")
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-i", str(video_path),
        "-f", "s16le",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            **get_subprocess_kwargs(),
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"FFmpeg audio decode failed: {proc.stderr[:200] if proc.stderr else 'No output'}")
        raw_bytes = proc.stdout
    except Exception as exc:
        logger.warning("Fast audio decode pipe failed (%s); returning evenly spaced clips", exc)
        return _build_fallback_moments(total_duration, clip_duration, top_k)

    # Convert PCM to float32 numpy array
    audio = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(audio) < sample_rate * 2:
        return _build_fallback_moments(total_duration, clip_duration, top_k)

    # Compute windowed RMS
    window_size = max(1, int(sample_rate * window_sec))
    num_windows = len(audio) // window_size
    if num_windows <= 0:
        return _build_fallback_moments(total_duration, clip_duration, top_k)

    reshaped = audio[: num_windows * window_size].reshape((num_windows, window_size))
    rms_values = np.sqrt(np.mean(reshaped ** 2, axis=1))

    # Convolve with sliding window corresponding to clip_duration
    clip_windows = max(1, int(clip_duration / window_sec))
    conv_kernel = np.ones(clip_windows, dtype=np.float32)
    energy_profile = np.convolve(rms_values, conv_kernel, mode="same")

    max_energy = float(np.max(energy_profile)) if len(energy_profile) > 0 else 1.0
    if max_energy <= 0.0001:
        return _build_fallback_moments(total_duration, clip_duration, top_k)

    # Iteratively pick top non-overlapping energy peaks
    picked: List[Tuple[float, float, float]] = []
    temp_profile = energy_profile.copy()
    min_suppress_windows = max(1, int(clip_duration / window_sec))

    for _ in range(top_k):
        best_idx = int(np.argmax(temp_profile))
        best_val = float(temp_profile[best_idx])
        if best_val <= 0.0:
            break

        peak_time = best_idx * window_sec
        # Center the clip window on peak with 35% lookback, 65% lookahead
        start_t = max(0.0, peak_time - (clip_duration * 0.35))
        end_t = min(total_duration, start_t + clip_duration)
        start_t = max(0.0, end_t - clip_duration)

        score = min(1.0, max(0.35, best_val / max_energy))
        picked.append((round(start_t, 2), round(end_t, 2), round(score, 3)))

        # Suppress neighborhood to avoid overlapping clips
        suppress_start = max(0, best_idx - min_suppress_windows)
        suppress_end = min(len(temp_profile), best_idx + min_suppress_windows)
        temp_profile[suppress_start:suppress_end] = -1.0

    if not picked:
        return _build_fallback_moments(total_duration, clip_duration, top_k)

    # Sort chronological for viewer comfort
    picked.sort(key=lambda x: x[0])
    return picked


def generate_sliding_window_moments(
    total_duration: float,
    clip_duration: float = 60.0,
    step_interval: float = 10.0,
) -> List[Tuple[float, float, float]]:
    """Generate chronological sliding window intervals (e.g. 0-60s, 10-70s, 20-80s)."""
    if total_duration <= clip_duration:
        return [(0.0, total_duration, 0.85)]

    if step_interval <= 0:
        step_interval = 10.0

    moments: List[Tuple[float, float, float]] = []
    t = 0.0
    while t + clip_duration <= total_duration + 0.5:
        start_t = round(t, 2)
        end_t = min(total_duration, round(t + clip_duration, 2))
        moments.append((start_t, end_t, 0.85))
        t += step_interval

    # If the last portion didn't reach the end, add final clip
    if moments and moments[-1][1] < total_duration - (step_interval * 0.5):
        final_start = max(0.0, total_duration - clip_duration)
        if final_start > moments[-1][0]:
            moments.append((round(final_start, 2), round(total_duration, 2), 0.85))

    return moments


def _build_fallback_moments(
    total_duration: float, clip_duration: float, count: int = 4
) -> List[Tuple[float, float, float]]:
    """Build evenly spaced intervals if audio analysis is unavailable."""
    if total_duration <= clip_duration:
        return [(0.0, total_duration, 0.75)]

    usable = total_duration - clip_duration
    step = usable / max(1, count)
    moments = []
    for i in range(count):
        st = i * step
        et = min(total_duration, st + clip_duration)
        moments.append((round(st, 2), round(et, 2), 0.70))
    return moments
