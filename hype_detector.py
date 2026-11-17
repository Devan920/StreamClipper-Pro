"""Hype / highlight detection for YouTube live stream clips.

Combines two signals — chat velocity spikes and audio RMS energy spikes —
to locate the most exciting moments in a live stream recording.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import pytchat  # only for type-checking; imported lazily at runtime

from config import HypeConfig
from video_renderer import _resolve_binary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class HypeEvent:
    """A single detected hype / highlight moment."""

    timestamp: float  # seconds from stream start
    score: float  # combined hype score (0-1 normalised)
    chat_score: float  # chat velocity component (0-1)
    audio_score: float  # audio energy component (0-1)
    segment_path: Path  # segment file that contains this moment
    segment_offset: float  # offset *within* that segment (seconds)


# ---------------------------------------------------------------------------
# Chat velocity analyser
# ---------------------------------------------------------------------------


class ChatAnalyzer:
    """Track YouTube live-chat message rate via *pytchat* and detect spikes.

    All public methods are thread-safe.  If *pytchat* is unavailable or the
    chat stream cannot be opened the analyser degrades gracefully: every query
    returns ``0.0`` and a warning is logged once.
    """

    _SAMPLE_RATE: float = 1.0  # seconds between rate samples

    def __init__(self, config: HypeConfig) -> None:
        self._config = config
        self._window_size: int = getattr(config, "chat_window_size", 30)
        self._spike_threshold: float = getattr(config, "chat_spike_threshold", 2.0)

        # Thread-safe sliding window: deque of (timestamp, msg_count_in_bucket)
        self._buckets: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

        self._running = False
        self._thread: threading.Thread | None = None
        self._chat: pytchat.LiveChat | None = None  # type: ignore[name-defined]
        self._available = True  # False if pytchat is missing / chat disabled

    # -- public API ----------------------------------------------------------

    def start(self, video_id: str) -> None:
        """Begin listening to the live chat for *video_id*.

        Spawns a daemon thread that continuously polls *pytchat*.
        """
        try:
            import pytchat as _pytchat  # noqa: WPS433 (nested import)
        except ImportError:
            logger.warning(
                "pytchat is not installed — chat velocity detection disabled."
            )
            self._available = False
            return

        try:
            self._chat = _pytchat.create(video_id=video_id)
        except Exception:
            logger.warning(
                "Failed to open live chat for %s — chat velocity disabled.",
                video_id,
                exc_info=True,
            )
            self._available = False
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="chat-analyzer",
            daemon=True,
        )
        self._thread.start()
        logger.info("ChatAnalyzer started for video %s", video_id)

    def stop(self) -> None:
        """Gracefully stop the polling thread."""
        self._running = False
        if self._chat is not None:
            try:
                self._chat.terminate()
            except Exception:
                logger.debug("Error terminating pytchat instance", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("ChatAnalyzer stopped.")

    def get_chat_velocity(self, timestamp: float) -> float:
        """Return normalised chat velocity at *timestamp* (0-1 range).

        The score is ``1.0`` when the current bucket rate is ≥ *spike_threshold*
        times the rolling average, ``0.0`` when it equals or is below the
        average, and linearly interpolated in between.
        """
        if not self._available:
            return 0.0

        with self._lock:
            if not self._buckets:
                return 0.0

            # Find the bucket closest to the requested timestamp.
            closest_count = 0
            closest_diff = float("inf")
            total = 0
            for ts, count in self._buckets:
                total += count
                diff = abs(ts - timestamp)
                if diff < closest_diff:
                    closest_diff = diff
                    closest_count = count

            avg = total / len(self._buckets) if self._buckets else 1.0
            if avg <= 0:
                return 0.0

            ratio = closest_count / avg
            # Normalise: 1.0 → 0, spike_threshold → 1, clamped to [0, 1]
            normalised = (ratio - 1.0) / (self._spike_threshold - 1.0)
            return float(np.clip(normalised, 0.0, 1.0))

    # -- internals -----------------------------------------------------------

    def _poll_loop(self) -> None:
        """Continuously pull chat items and bucket-ise them."""
        import time

        bucket_start = time.monotonic()
        bucket_count = 0

        while self._running and self._chat is not None and self._chat.is_alive():
            try:
                items = self._chat.get()
                for _ in items.sync_items():
                    bucket_count += 1
            except Exception:
                logger.debug("pytchat poll error", exc_info=True)
                break

            now = time.monotonic()
            if now - bucket_start >= self._SAMPLE_RATE:
                with self._lock:
                    self._buckets.append((now, bucket_count))
                    # Trim to window size (in buckets).
                    while len(self._buckets) > self._window_size:
                        self._buckets.popleft()
                bucket_count = 0
                bucket_start = now

        logger.debug("Chat poll loop exited.")


# ---------------------------------------------------------------------------
# Audio RMS analyser
# ---------------------------------------------------------------------------


class AudioAnalyzer:
    """Extract per-window RMS energy from segment files using *ffmpeg*."""

    _SAMPLE_RATE: int = 16_000  # Hz – mono f32le
    _SAMPLE_WIDTH: int = 4  # bytes per float32 sample

    def __init__(self, config: HypeConfig) -> None:
        self._config = config
        self._window_sec: float = getattr(config, "audio_window_sec", 0.5)
        self._spike_threshold: float = getattr(config, "audio_spike_threshold", 2.0)

    async def analyze_segment(
        self, segment_path: Path
    ) -> list[tuple[float, float]]:
        """Return a list of ``(offset_seconds, rms_energy)`` values.

        Each entry corresponds to one non-overlapping window of
        *audio_window_sec* seconds.  Uses *ffmpeg* to decode audio to raw
        PCM (mono 16 kHz float32-LE) and *numpy* for the RMS computation.

        Returns an empty list on any ffmpeg / I/O error.
        """
        if not segment_path.is_file():
            logger.warning("Segment file does not exist: %s", segment_path)
            return []

        try:
            raw_pcm = await self._extract_pcm(segment_path)
        except Exception:
            logger.error(
                "Failed to extract PCM from %s", segment_path, exc_info=True
            )
            return []

        if not raw_pcm:
            logger.warning("Empty PCM output for %s", segment_path)
            return []

        return self._compute_rms_windows(raw_pcm)

    # -- internals -----------------------------------------------------------

    async def _extract_pcm(self, segment_path: Path) -> bytes:
        """Run ffmpeg and return raw f32le PCM bytes."""
        cmd = [
            _resolve_binary("ffmpeg"),
            "-nostdin",
            "-y",
            "-i", str(segment_path),
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ac", "1",
            "-ar", str(self._SAMPLE_RATE),
            "-",
        ]
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kw,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(
                "ffmpeg exited with code %d: %s",
                proc.returncode,
                stderr.decode(errors="replace")[:500],
            )
            return b""
        return stdout

    def _compute_rms_windows(
        self, raw_pcm: bytes
    ) -> list[tuple[float, float]]:
        """Split raw PCM into windows and compute RMS for each."""
        # Convert bytes → numpy float32 array
        n_samples = len(raw_pcm) // self._SAMPLE_WIDTH
        if n_samples == 0:
            return []

        samples = np.frombuffer(raw_pcm[:n_samples * self._SAMPLE_WIDTH], dtype=np.float32)

        window_samples = int(self._window_sec * self._SAMPLE_RATE)
        if window_samples <= 0:
            window_samples = self._SAMPLE_RATE  # fallback: 1-second window

        results: list[tuple[float, float]] = []
        for start_idx in range(0, len(samples), window_samples):
            chunk = samples[start_idx : start_idx + window_samples]
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            offset_sec = start_idx / self._SAMPLE_RATE
            results.append((offset_sec, rms))

        return results


# ---------------------------------------------------------------------------
# Combined hype detector
# ---------------------------------------------------------------------------


class HypeDetector:
    """Combine chat velocity and audio energy to locate hype moments."""

    _CHAT_WEIGHT: float = 0.4
    _AUDIO_WEIGHT: float = 0.6

    def __init__(self, config: HypeConfig) -> None:
        self._config = config
        self._chat = ChatAnalyzer(config)
        self._audio = AudioAnalyzer(config)

        self._hype_threshold: float = getattr(config, "hype_threshold", 0.5)
        self._min_gap: float = getattr(config, "min_gap_between_events", 30.0)
        self._lookback: float = getattr(config, "clip_lookback", 10.0)
        self._lookahead: float = getattr(config, "clip_lookahead", 10.0)
        self._clip_duration: float = getattr(config, "clip_duration", 60.0)

    # -- public helpers ------------------------------------------------------

    @property
    def chat_analyzer(self) -> ChatAnalyzer:
        """Expose the underlying :class:`ChatAnalyzer` for starting/stopping."""
        return self._chat

    @property
    def audio_analyzer(self) -> AudioAnalyzer:
        """Expose the underlying :class:`AudioAnalyzer`."""
        return self._audio

    # -- core analysis -------------------------------------------------------

    async def analyze_segment(
        self,
        segment_path: Path,
        segment_start_time: float,
    ) -> list[HypeEvent]:
        """Analyse a single segment and return any detected :class:`HypeEvent` s.

        Parameters
        ----------
        segment_path:
            Path to the media segment file.
        segment_start_time:
            Absolute time (seconds from stream start) when the segment begins.

        Returns
        -------
        list[HypeEvent]
            Detected hype events sorted by timestamp, with a minimum gap of
            *min_gap_between_events* enforced between consecutive events.
        """
        # 1. Audio analysis
        try:
            rms_data = await self._audio.analyze_segment(segment_path)
        except Exception:
            logger.error(
                "Audio analysis failed for %s — skipping segment.",
                segment_path,
                exc_info=True,
            )
            rms_data = []

        if not rms_data:
            logger.debug("No audio data for %s — no events produced.", segment_path)
            return []

        # 2. Normalise audio RMS values within this segment.
        rms_values = np.array([rms for _, rms in rms_data], dtype=np.float64)
        rms_max = float(rms_values.max()) if rms_values.max() > 0 else 1.0
        audio_normed = rms_values / rms_max  # 0-1

        # 3. Build per-window combined scores.
        candidates: list[HypeEvent] = []
        for idx, (offset, _rms) in enumerate(rms_data):
            abs_time = segment_start_time + offset
            audio_score = float(audio_normed[idx])
            chat_score = self._chat.get_chat_velocity(abs_time)
            combined = self._CHAT_WEIGHT * chat_score + self._AUDIO_WEIGHT * audio_score

            if combined >= self._hype_threshold:
                candidates.append(
                    HypeEvent(
                        timestamp=abs_time,
                        score=combined,
                        chat_score=chat_score,
                        audio_score=audio_score,
                        segment_path=segment_path,
                        segment_offset=offset,
                    )
                )

        # 4. Sort by score (descending) and enforce minimum gap.
        candidates.sort(key=lambda e: e.score, reverse=True)
        events = self._enforce_min_gap(candidates)

        # Final sort by timestamp for downstream consumers.
        events.sort(key=lambda e: e.timestamp)
        logger.info(
            "Detected %d hype events in %s (threshold=%.2f)",
            len(events),
            segment_path.name,
            self._hype_threshold,
        )
        return events

    def get_clip_boundaries(self, event: HypeEvent) -> tuple[float, float]:
        """Return ``(start_time, end_time)`` for a clip centred on *event*.

        The clip extends *lookback* seconds before and *lookahead* seconds
        after the event timestamp, but is clamped so the total duration never
        exceeds *clip_duration* and *start_time* is never negative.
        """
        raw_start = event.timestamp - self._lookback
        raw_end = event.timestamp + self._lookahead

        # Clamp total length to clip_duration.
        duration = raw_end - raw_start
        if duration > self._clip_duration:
            excess = duration - self._clip_duration
            # Trim symmetrically.
            raw_start += excess / 2
            raw_end -= excess / 2

        # Never go below zero.
        start = max(0.0, raw_start)
        end = start + min(raw_end - raw_start, self._clip_duration)
        return (start, end)

    # -- internals -----------------------------------------------------------

    def _enforce_min_gap(self, candidates: list[HypeEvent]) -> list[HypeEvent]:
        """Greedily keep highest-scoring events that are ≥ *min_gap* apart."""
        kept: list[HypeEvent] = []
        for candidate in candidates:
            too_close = any(
                abs(candidate.timestamp - k.timestamp) < self._min_gap
                for k in kept
            )
            if not too_close:
                kept.append(candidate)
        return kept
