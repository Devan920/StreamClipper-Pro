"""Live stream ingestor using yt-dlp + ffmpeg.

Records a live YouTube stream into rolling fixed-duration segments by
piping the stream URL extracted via yt-dlp into ffmpeg's segment muxer.
Provides an async generator that yields completed segment paths.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, AsyncGenerator

import yt_dlp  # type: ignore[import-untyped]

from config import IngestorConfig, get_segments_dir
from video_renderer import _resolve_binary, get_subprocess_kwargs

logger = logging.getLogger(__name__)

_SEGMENT_POLL_INTERVAL: float = 1.0  # seconds between directory polls
_RECONNECT_DELAY: float = 5.0  # seconds to wait before reconnecting
_MAX_RECONNECT_ATTEMPTS: int = 10


class StreamIngestor:
    """Ingests a live stream into rolling fixed-duration segments.

    Uses yt-dlp to resolve the direct stream URL and ffmpeg's ``-f segment``
    muxer to split the recording into sequential MPEG-TS (.ts) or MP4 files.
    Old segments beyond *max_segments* are automatically deleted.

    Parameters
    ----------
    url:
        YouTube (or yt-dlp–compatible) live stream URL.
    config:
        Ingestor-specific configuration dataclass.
    log_callback:
        Optional callback for forwarding log lines to the UI (message, level).
    """

    def __init__(
        self,
        url: str,
        config: IngestorConfig | None = None,
        log_callback: Any = None,
    ) -> None:
        self._url = url
        self._config = config or IngestorConfig()
        self._log_callback = log_callback
        self._process: subprocess.Popen[bytes] | None = None
        self._stop_event = asyncio.Event()
        self._segments: deque[Path] = deque()
        self._lock = threading.Lock()

    def _log(self, message: str, level: str = "INFO") -> None:
        if level == "ERROR":
            logger.error(message)
        elif level == "WARNING":
            logger.warning(message)
        elif level == "DEBUG":
            logger.debug(message)
        else:
            logger.info(message)

        if self._log_callback:
            try:
                self._log_callback(message, level)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> AsyncGenerator[Path, None]:
        """Start recording and yield segment paths as they complete.

        Automatically reconnects when the stream drops or ffmpeg exits
        unexpectedly, up to ``_MAX_RECONNECT_ATTEMPTS`` consecutive
        failures.

        Yields
        ------
        Path
            Absolute path to each completed segment file.
        """
        self._config.output_dir.mkdir(parents=True, exist_ok=True)
        reconnect_attempts = 0

        while not self._stop_event.is_set():
            try:
                stream_url = await asyncio.to_thread(self._extract_stream_url)
                logger.info("Resolved stream URL, starting ffmpeg recording.")
                self._process = await asyncio.to_thread(
                    self._spawn_ffmpeg, stream_url
                )
                reconnect_attempts = 0  # reset on successful start

                async for segment_path in self._watch_segments():
                    yield segment_path

            except _StreamExtractionError as exc:
                logger.error("Stream extraction failed: %s", exc)
            except Exception:
                logger.exception("Unexpected error in ingestor loop.")
            finally:
                self._terminate_ffmpeg()

            if self._stop_event.is_set():
                break

            reconnect_attempts += 1
            if reconnect_attempts > _MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    "Exceeded %d reconnect attempts — giving up.",
                    _MAX_RECONNECT_ATTEMPTS,
                )
                break

            logger.warning(
                "Reconnecting in %.0fs (attempt %d/%d)…",
                _RECONNECT_DELAY,
                reconnect_attempts,
                _MAX_RECONNECT_ATTEMPTS,
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=_RECONNECT_DELAY
                )
                # stop_event was set during the wait — break out
                break
            except asyncio.TimeoutError:
                pass  # delay elapsed, retry

        logger.info("Ingestor shut down.")

    async def stop(self) -> None:
        """Gracefully stop recording and clean up."""
        logger.info("Stop requested — shutting down ingestor.")
        self._stop_event.set()
        self._terminate_ffmpeg()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_stream_url(self) -> str:
        """Use yt-dlp to extract the direct HLS/DASH manifest URL.

        Returns
        -------
        str
            Direct stream URL suitable for feeding into ffmpeg.

        Raises
        ------
        _StreamExtractionError
            If yt-dlp cannot resolve the URL.
        """
        format_selector = self._config.video_quality
        if not format_selector or format_selector == "best":
            format_selector = "best[protocol^=m3u8]/bestvideo+bestaudio/best"

        ydl_opts: dict[str, object] = {
            "format": format_selector,
            "quiet": True,
            "no_warnings": True,
            "simulate": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web"]
                }
            },
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, object] = ydl.extract_info(
                    self._url, download=False
                )  # type: ignore[assignment]

                if info is None:
                    raise _StreamExtractionError("yt-dlp returned no info.")

                url = str(info.get("url") or info.get("manifest_url", ""))
                if not url:
                    # Check formats list for live m3u8 or best playable URL
                    formats = info.get("formats")
                    if isinstance(formats, list) and formats:
                        for f in reversed(formats):
                            if isinstance(f, dict):
                                proto = str(f.get("protocol", "")).lower()
                                f_url = str(f.get("url") or f.get("manifest_url", ""))
                                if f_url and ("m3u8" in proto or "m3u8" in f_url or proto.startswith("http")):
                                    url = f_url
                                    break
                        if not url:
                            url = str(formats[-1].get("url", ""))

                if not url:
                    raise _StreamExtractionError(
                        "Could not resolve a direct stream URL from yt-dlp info."
                    )

                logger.info("Extracted stream URL: %s", url[:120])
                return url

        except yt_dlp.utils.DownloadError as exc:
            # Fallback attempt with generic format if initial selector failed
            if "format" in str(exc).lower():
                logger.warning("Primary format selector failed, attempting fallback format...")
                try:
                    fallback_opts = dict(ydl_opts)
                    fallback_opts["format"] = "best/b"
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                        info = ydl_fb.extract_info(self._url, download=False)
                        if info:
                            url = str(info.get("url") or info.get("manifest_url", ""))
                            if url:
                                logger.info("Fallback succeeded, URL: %s", url[:120])
                                return url
                except Exception:
                    pass
            raise _StreamExtractionError(str(exc)) from exc

    def _spawn_ffmpeg(self, stream_url: str) -> subprocess.Popen[bytes]:
        """Spawn an ffmpeg subprocess that records into rolling segments.

        Parameters
        ----------
        stream_url:
            Direct stream URL (HLS/DASH manifest).

        Returns
        -------
        subprocess.Popen
            The running ffmpeg process.
        """
        fmt = (self._config.format or "ts").lower()
        if fmt in ("ts", "mpegts"):
            ext = "ts"
            segment_format = "mpegts"
        else:
            ext = fmt
            segment_format = fmt

        output_pattern = str(
            self._config.output_dir / f"seg_%Y%m%d_%H%M%S.{ext}"
        )

        ffmpeg_bin = _resolve_binary("ffmpeg")

        cmd: list[str] = [
            ffmpeg_bin,
            "-loglevel", self._config.ffmpeg_loglevel,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", stream_url,
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self._config.segment_duration),
            "-segment_format", segment_format,
            "-reset_timestamps", "1",
            "-strftime", "1",
            output_pattern,
        ]

        self._log(
            f"🎬 Ingest recording: {self._config.segment_duration}s .{ext} chunks -> {self._config.output_dir}",
            "INFO",
        )
        logger.debug("Spawning ffmpeg: %s", " ".join(cmd))

        kw = get_subprocess_kwargs()
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **kw,
        )
        self._log(f"FFmpeg ingest started (PID {process.pid})", "DEBUG")

        # Daemon thread to forward stderr lines directly to the UI live log
        def _read_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for raw_line in iter(process.stderr.readline, b""):
                    if not raw_line or self._stop_event.is_set():
                        break
                    line = raw_line.decode(errors="replace").strip()
                    if not line:
                        continue
                    lower = line.lower()
                    if any(err in lower for err in [
                        "error", "invalid", "fatal", "failed", "could not",
                        "permission denied", "access is denied", "no such file",
                    ]):
                        self._log(f"⚠️ [FFmpeg] {line}", "ERROR")
                    elif any(warn in lower for warn in ["warning", "drop", "corrupt", "reconnect"]):
                        self._log(f"[FFmpeg] {line}", "WARNING")
                    else:
                        logger.debug("[FFmpeg] %s", line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()
        return process

    async def _watch_segments(self) -> AsyncGenerator[Path, None]:
        """Poll the output directory for newly completed segments.

        A segment is considered *complete* once a newer segment appears
        (i.e., ffmpeg has moved on to writing the next file).

        Yields
        ------
        Path
            Absolute path to each completed segment.
        """
        fmt = (self._config.format or "ts").lower()
        ext = "ts" if fmt in ("ts", "mpegts") else fmt
        pattern = f"*.{ext}"

        seen: set[str] = {
            p.name for p in self._config.output_dir.glob(pattern)
        }
        pending: Path | None = None

        while not self._stop_event.is_set():
            # Check if ffmpeg is still alive.
            if self._process is not None and self._process.poll() is not None:
                ret = self._process.returncode or 0
                if ret != 0:
                    self._log(f"⚠️ FFmpeg ingest exited with code {ret}.", "ERROR")
                else:
                    self._log(f"FFmpeg ingest completed with code {ret}.", "INFO")
                # Yield the last pending segment if any and non-empty.
                if pending is not None and pending.exists() and pending.stat().st_size > 0:
                    yield pending
                    self._track_segment(pending)
                    pending = None
                return

            current_files = sorted(
                self._config.output_dir.glob(pattern),
                key=lambda p: p.stat().st_mtime,
            )
            new_files = [f for f in current_files if f.name not in seen]

            for new_file in new_files:
                seen.add(new_file.name)
                # The previously pending file is now complete because
                # ffmpeg has started writing a new one.
                if pending is not None and pending.exists() and pending.stat().st_size > 0:
                    self._log(f"Segment complete: {pending.name} ({pending.stat().st_size / 1e6:.2f} MB)", "DEBUG")
                    yield pending
                    self._track_segment(pending)
                pending = new_file

            await asyncio.sleep(_SEGMENT_POLL_INTERVAL)

    def _track_segment(self, segment: Path) -> None:
        """Track a completed segment and clean up old ones."""
        with self._lock:
            self._segments.append(segment)
        self._cleanup_old_segments()

    def _cleanup_old_segments(self) -> None:
        """Remove segments that exceed the rolling buffer limit."""
        with self._lock:
            while len(self._segments) > self._config.max_segments:
                oldest = self._segments.popleft()
                try:
                    oldest.unlink(missing_ok=True)
                    logger.debug("Deleted old segment: %s", oldest.name)
                except OSError:
                    logger.warning(
                        "Failed to delete old segment: %s", oldest.name,
                        exc_info=True,
                    )

    def _terminate_ffmpeg(self) -> None:
        """Terminate the ffmpeg subprocess if it is still running."""
        if self._process is None:
            return

        if self._process.poll() is None:
            logger.info("Terminating ffmpeg (PID %d).", self._process.pid)
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except (subprocess.TimeoutExpired, Exception):
                logger.warning("ffmpeg did not exit in time — killing.")
                try:
                    self._process.kill()
                    self._process.wait(timeout=3)
                except Exception:
                    pass

        try:
            if self._process.stderr is not None:
                self._process.stderr.close()
        except Exception:
            pass

        self._process = None
            self._process.stderr.close()

        self._process = None


class _StreamExtractionError(Exception):
    """Raised when yt-dlp fails to extract a stream URL."""
