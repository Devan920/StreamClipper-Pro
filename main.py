#!/usr/bin/env python3
"""
Stream Clipper — Real-time YouTube Live Stream Clipping Pipeline

Orchestrates the full pipeline:
  1. Ingest live stream into rolling 60-second segments
  2. Detect hype moments via chat velocity + audio energy
  3. Transcribe hype clips with word-level timestamps
  4. Edit clips to 9:16 vertical with stylish animated captions

Usage:
  python main.py <youtube_url> [--clip-duration 60] [--model-size base] [--output-dir output]
  python main.py --from-vod <youtube_url>       # Process a VOD instead of live stream
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Ensure the package directory is on sys.path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    EditorConfig,
    HypeConfig,
    IngestorConfig,
    PipelineConfig,
    TranscriberConfig,
)
from vod_processor import get_subprocess_kwargs
from hype_detector import HypeDetector, HypeEvent
from stream_ingestor import StreamIngestor
from transcriber import Transcriber
from video_editor import VideoEditor

logger = logging.getLogger("stream_clipper.main")


# ─── VOD Mode (process a non-live video) ─────────────────────────────────────

async def process_vod(url: str, config: PipelineConfig) -> list[Path]:
    """
    Download a VOD / uploaded video, detect highlights, and produce clips.

    This is the simpler path: download the full video first, then run
    hype detection, transcription, and editing on it.
    """
    import subprocess
    import tempfile

    logger.info("VOD mode — downloading video: %s", url)

    # Download with yt-dlp
    download_dir = Path(tempfile.mkdtemp(prefix="stream_clipper_vod_"))
    output_template = str(download_dir / "vod.%(ext)s")

    try:
        import yt_dlp

        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"]
                }
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id", "unknown")
    except Exception as dl_err:
        # Fallback to CLI
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
            "--extractor-args", "youtube:player_client=android,web",
            "-o", output_template,
            "--merge-output-format", "mp4",
            "-q",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
        video_id = "unknown"

    # Find the downloaded file
    downloaded = list(download_dir.glob("vod.*"))
    if not downloaded:
        raise FileNotFoundError(f"No downloaded file found in {download_dir}")
    vod_path = downloaded[0]
    logger.info("Downloaded VOD to %s (%.1f MB)", vod_path, vod_path.stat().st_size / 1e6)

    # Get video duration via ffprobe
    duration = _get_duration(vod_path)
    logger.info("Video duration: %.1f seconds", duration)

    # Segment the video into chunks for analysis
    segment_duration = config.ingestor.segment_duration
    segments: list[tuple[Path, float]] = []

    segment_dir = Path(config.ingestor.output_dir)
    segment_dir.mkdir(parents=True, exist_ok=True)

    for seg_start in range(0, int(duration), segment_duration):
        seg_end = min(seg_start + segment_duration, duration)
        seg_path = segment_dir / f"vod_seg_{seg_start:05d}.mp4"

        if not seg_path.exists():
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seg_start),
                "-i", str(vod_path),
                "-t", str(seg_end - seg_start),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(seg_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True, **get_subprocess_kwargs())

        segments.append((seg_path, seg_start))

    logger.info("Split VOD into %d segments", len(segments))

    # Detect hype moments across all segments
    detector = HypeDetector(config.hype)
    all_events: list[HypeEvent] = []

    for seg_path, seg_start in segments:
        events = await detector.analyze_segment(seg_path, seg_start)
        all_events.extend(events)

    if not all_events:
        logger.warning("No hype events detected — selecting evenly-spaced clips instead")
        # Fallback: pick clips at 25%, 50%, 75% of the video
        for pct in [0.25, 0.50, 0.75]:
            t = duration * pct
            all_events.append(
                HypeEvent(
                    timestamp=t,
                    score=0.5,
                    chat_score=0.0,
                    audio_score=0.5,
                    segment_path=vod_path,
                    segment_offset=t,
                )
            )

    # Sort by score descending, take top N
    all_events.sort(key=lambda e: e.score, reverse=True)
    top_events = all_events[:3]
    logger.info(
        "Top %d hype events: %s",
        len(top_events),
        [(f"{e.timestamp:.1f}s", f"score={e.score:.2f}") for e in top_events],
    )

    # Process each hype event into a clip
    transcriber = Transcriber(config.transcriber)
    editor = VideoEditor(config.editor)
    output_paths: list[Path] = []

    for i, event in enumerate(top_events, 1):
        clip_start, clip_end = detector.get_clip_boundaries(event)
        clip_start = max(0, clip_start)
        clip_end = min(duration, clip_end)
        clip_duration = clip_end - clip_start

        logger.info(
            "Processing clip %d/%d: %.1fs → %.1fs (%.1fs, score=%.2f)",
            i, len(top_events), clip_start, clip_end, clip_duration, event.score,
        )

        # Transcribe the clip segment
        try:
            transcript = transcriber.transcribe_segment(vod_path, clip_start, clip_end)
            logger.info(
                "Transcribed %d segments, %d words",
                len(transcript.segments),
                sum(len(s.words) for s in transcript.segments),
            )
        except Exception as e:
            logger.error("Transcription failed for clip %d: %s", i, e)
            continue

        # Create the edited vertical clip with captions
        try:
            output_name = f"clip_{i:02d}_t{int(clip_start)}"
            output_path = editor.create_clip(
                source_path=vod_path,
                start=clip_start,
                end=clip_end,
                transcript=transcript,
                output_name=output_name,
            )
            output_paths.append(output_path)
            logger.info("✅ Clip %d saved to %s", i, output_path)
        except Exception as e:
            logger.error("Editing failed for clip %d: %s", i, e)
            continue

    return output_paths


# ─── Live Mode ────────────────────────────────────────────────────────────────

async def process_live(url: str, config: PipelineConfig) -> None:
    """
    Process a live stream in real-time.

    Continuously ingests segments, analyzes them for hype moments, and
    automatically creates clips when highlights are detected.
    """
    ingestor = StreamIngestor(url, config.ingestor)
    detector = HypeDetector(config.hype)
    transcriber = Transcriber(config.transcriber)
    editor = VideoEditor(config.editor)

    clip_count = 0
    segment_index = 0
    stream_start_time = time.time()

    logger.info("Starting live stream processing: %s", url)

    try:
        async for segment_path in ingestor.start():
            segment_start_time = segment_index * config.ingestor.segment_duration
            segment_index += 1

            logger.info(
                "Analyzing segment %d: %s (stream time: %ds)",
                segment_index, segment_path.name, segment_start_time,
            )

            # Detect hype in this segment
            try:
                events = await detector.analyze_segment(segment_path, segment_start_time)
            except Exception as e:
                logger.error("Hype detection failed for %s: %s", segment_path.name, e)
                continue

            if not events:
                logger.debug("No hype events in segment %d", segment_index)
                continue

            # Process each hype event
            for event in events:
                clip_count += 1
                clip_start, clip_end = detector.get_clip_boundaries(event)

                logger.info(
                    "🔥 Hype event detected! score=%.2f at stream time %.1fs",
                    event.score, event.timestamp,
                )

                # Transcribe
                try:
                    transcript = transcriber.transcribe_segment(
                        segment_path,
                        event.segment_offset - config.hype.lookback_seconds,
                        event.segment_offset + config.hype.lookahead_seconds,
                    )
                except Exception as e:
                    logger.error("Transcription failed: %s", e)
                    continue

                # Edit and produce clip
                try:
                    output_name = (
                        f"live_clip_{clip_count:03d}_t{int(event.timestamp)}"
                    )
                    output_path = editor.create_clip(
                        source_path=segment_path,
                        start=max(0, event.segment_offset - config.hype.lookback_seconds),
                        end=min(
                            config.ingestor.segment_duration,
                            event.segment_offset + config.hype.lookahead_seconds,
                        ),
                        transcript=transcript,
                        output_name=output_name,
                    )
                    logger.info("✅ Live clip %d saved to %s", clip_count, output_path)
                except Exception as e:
                    logger.error("Clip editing failed: %s", e)

    except KeyboardInterrupt:
        logger.info("Stopping live stream processing (user interrupt)")
    finally:
        await ingestor.stop()
        elapsed = time.time() - stream_start_time
        logger.info(
            "Session ended. Processed %d segments, created %d clips in %.0f seconds",
            segment_index, clip_count, elapsed,
        )


# ─── Utilities ────────────────────────────────────────────────────────────────

def _get_duration(path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    import subprocess

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def _extract_video_id(url: str) -> str | None:
    """Extract the YouTube video ID from a URL."""
    import re

    patterns = [
        r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream Clipper — Real-time YouTube Live Stream Clipping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a live stream in real-time
  python main.py https://www.youtube.com/watch?v=LIVE_ID

  # Process a VOD / uploaded video
  python main.py --from-vod https://youtu.be/ycjB9IWTQL0

  # Custom settings
  python main.py --from-vod URL --clip-duration 30 --model-size small --output-dir clips
        """,
    )
    parser.add_argument("url", help="YouTube video/stream URL")
    parser.add_argument(
        "--from-vod", action="store_true",
        help="Process as a VOD (uploaded video) instead of live stream",
    )
    parser.add_argument(
        "--aspect-ratio", default="9:16",
        choices=["9:16", "16:9", "1:1", "4:5"],
        help="Target video aspect ratio (default: 9:16)",
    )
    parser.add_argument(
        "--clip-duration", type=int, default=60, choices=[30, 60],
        help="Target clip duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--segment-duration", type=int, default=60,
        help="Segment duration for stream ingestion (default: 60)",
    )
    parser.add_argument(
        "--model-size", default="base",
        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
        help="Whisper model size for transcription (default: base)",
    )
    parser.add_argument(
        "--language", default=None,
        help="Force transcription language (e.g., 'en', 'ml'). Auto-detect if omitted.",
    )
    parser.add_argument(
        "--output-dir", default="output",
        help="Directory for output clips (default: output)",
    )
    parser.add_argument(
        "--max-clips", type=int, default=3,
        help="Maximum number of clips to generate in VOD mode (default: 3)",
    )
    parser.add_argument(
        "--font-size", type=int, default=58,
        help="Caption font size (default: 58)",
    )
    parser.add_argument(
        "--highlight-color", default="#FFD700",
        help="Caption highlight color for active word (default: #FFD700 gold)",
    )
    parser.add_argument(
        "--no-captions", action="store_true",
        help="Skip caption generation",
    )
    parser.add_argument(
        "--quality", default="1080p Ultra (High Bitrate)",
        choices=["1080p Ultra (High Bitrate)", "1080p Standard", "720p Fast"],
        help="Export video quality (default: 1080p Ultra)",
    )
    parser.add_argument(
        "--no-face-tracking", action="store_true",
        help="Disable auto face tracking (use center-crop instead)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose/debug logging",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> PipelineConfig:
    """Build PipelineConfig from CLI arguments."""
    ratio_map = {
        "9:16": (1080, 1920, 380, 1.0),
        "16:9": (1920, 1080, 110, 0.75),
        "1:1": (1080, 1080, 140, 0.82),
        "4:5": (1080, 1350, 220, 0.90),
    }
    w, h, margin_v, font_scale = ratio_map.get(args.aspect_ratio, (1080, 1920, 380, 1.0))

    quality_map = {
        "1080p Ultra (High Bitrate)": (1.0, 17, "medium", "192k"),
        "1080p Standard": (1.0, 21, "fast", "160k"),
        "720p Fast": (720.0 / 1080.0, 23, "veryfast", "128k"),
    }
    q_scale, crf, preset, ab = quality_map.get(args.quality, (1.0, 17, "medium", "192k"))

    final_w = int(w * q_scale) - (int(w * q_scale) % 2)
    final_h = int(h * q_scale) - (int(h * q_scale) % 2)

    config = PipelineConfig(
        ingestor=IngestorConfig(
            segment_duration=args.segment_duration,
            output_dir=Path(args.output_dir).parent / "segments",
        ),
        hype=HypeConfig(
            clip_duration=args.clip_duration,
        ),
        transcriber=TranscriberConfig(
            model_size=args.model_size,
            language=args.language,
        ),
        editor=EditorConfig(
            output_dir=Path(args.output_dir),
            output_width=final_w,
            output_height=final_h,
            margin_v=max(40, int(margin_v * q_scale)),
            font_size=max(24, int(args.font_size * font_scale * q_scale)),
            highlight_color=args.highlight_color,
            enable_face_tracking=not args.no_face_tracking and args.aspect_ratio != "16:9",
            quality=args.quality,
            crf=crf,
            preset=preset,
            audio_bitrate=ab,
        ),
        log_level="DEBUG" if args.verbose else "INFO",
    )
    config.setup_logging()
    return config


async def main() -> int:
    args = parse_args()
    config = build_config(args)

    logger.info("=" * 60)
    logger.info("  Stream Clipper — YouTube Live Clipping Pipeline")
    logger.info("=" * 60)
    logger.info("URL:            %s", args.url)
    logger.info("Mode:           %s", "VOD" if args.from_vod else "Live")
    logger.info("Clip duration:  %ds", args.clip_duration)
    logger.info("Whisper model:  %s", args.model_size)
    logger.info("Output dir:     %s", args.output_dir)
    logger.info("=" * 60)

    try:
        if args.from_vod:
            clips = await process_vod(args.url, config)
            if clips:
                logger.info("=" * 60)
                logger.info("🎬 Generated %d clip(s):", len(clips))
                for clip in clips:
                    size_mb = clip.stat().st_size / 1e6 if clip.exists() else 0
                    logger.info("   → %s (%.1f MB)", clip, size_mb)
                logger.info("=" * 60)
            else:
                logger.warning("No clips were generated.")
                return 1
        else:
            await process_live(args.url, config)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
