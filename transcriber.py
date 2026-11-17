"""Transcription module using faster-whisper for word-level timestamps.

Provides the :class:`Transcriber` facade that lazily loads a
``faster_whisper.WhisperModel`` and exposes helpers for full-file and
sub-range transcription with structured dataclass results.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import requests

from config import TranscriberConfig
from video_renderer import _resolve_binary, get_subprocess_kwargs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts", ".m2ts",
})


@dataclass
class Word:
    """A single recognised word with timing and confidence."""

    text: str
    start: float  # seconds
    end: float  # seconds
    confidence: float


@dataclass
class Segment:
    """A contiguous speech segment containing one or more words."""

    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    """Complete transcription result with segment / word detail."""

    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    duration: float = 0.0

    # -- serialisation helpers -----------------------------------------------

    def to_srt(self) -> str:
        """Convert the transcript to SRT subtitle format.

        Returns:
            A string containing the complete SRT file content.
        """
        lines: list[str] = []
        for idx, seg in enumerate(self.segments, start=1):
            start_ts = _seconds_to_srt_time(seg.start)
            end_ts = _seconds_to_srt_time(seg.end)
            lines.append(str(idx))
            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(seg.text.strip())
            lines.append("")  # blank line between cues
        return "\n".join(lines)

    def to_word_json(self) -> list[dict]:
        """Export word-level timestamps as a JSON-serialisable list.

        Returns:
            List of dicts, each with keys ``text``, ``start``, ``end``,
            ``confidence``.
        """
        return [
            {
                "text": w.text,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "confidence": round(w.confidence, 4),
            }
            for seg in self.segments
            for w in seg.words
        ]

    def get_words_in_range(self, start: float, end: float) -> list[Word]:
        """Return all words whose interval overlaps ``[start, end]``.

        Args:
            start: Range start in seconds.
            end: Range end in seconds.

        Returns:
            List of :class:`Word` objects within the time range.
        """
        return [
            w
            for seg in self.segments
            for w in seg.words
            if w.end >= start and w.start <= end
        ]



def clean_and_clamp_words(words: list[Word]) -> list[Word]:
    """Clean punctuation artifacts, merge split contractions, and clamp timing to prevent drift or overlap."""
    if not words:
        return []

    cleaned: list[Word] = []
    # Common contraction suffixes that frequently get split by speech tokenizers
    contraction_suffixes = {
        "'t", "'s", "'m", "'re", "'ve", "'ll", "'d",
        "n't", "’t", "’s", "’m", "’re", "’ve", "’ll", "’d",
    }

    for w in words:
        txt = str(w.text or "").strip()
        if not txt:
            continue

        # Standardize curly apostrophes
        txt = txt.replace("’", "'").replace("`", "'")
        txt = re.sub(r"\s*'\s*", "'", txt)

        # If token is a contraction suffix, merge with the preceding word
        lower_txt = txt.lower()
        if cleaned and (lower_txt in contraction_suffixes or lower_txt.startswith("'")):
            prev = cleaned[-1]
            prev.text = f"{prev.text}{txt}"
            prev.end = max(prev.end, w.end)
            prev.confidence = min(prev.confidence, w.confidence)
            continue

        # Strip stray non-alphanumeric edges except valid currency or symbols ($100, #1, 50%)
        # Retain apostrophes inside words like "DON'T"
        stripped = re.sub(r"^[\s\"'.,;:!?\-_~]+|[\s\"'.,;:!?\-_~]+$", "", txt)
        if not stripped:
            continue

        cleaned_word = Word(
            text=stripped,
            start=max(0.0, float(w.start)),
            end=max(0.0, float(w.end)),
            confidence=float(w.confidence),
        )
        cleaned.append(cleaned_word)

    if not cleaned:
        return []

    # Clamp timings so words strictly progress forwards with no negative duration or overlaps
    for i in range(len(cleaned)):
        cw = cleaned[i]
        cw.start = max(0.0, cw.start)
        cw.end = max(cw.start + 0.05, cw.end)
        if i > 0:
            pw = cleaned[i - 1]
            if pw.end > cw.start:
                if cw.start - pw.start >= 0.05:
                    pw.end = cw.start
                else:
                    cw.start = pw.end
                    cw.end = max(cw.start + 0.05, cw.end)

    return cleaned


def sanitize_gemini_word(text: str) -> str:
    """Sanitize word returned by Gemini: strip markdown, braces, backslashes, newlines."""
    if not text:
        return ""
    t = re.sub(r"[\*`#~_<>{}\[\]\\]", "", str(text))
    t = t.replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip("\"'")


def refine_transcript_with_gemini(
    transcript: Transcript,
    api_key: str,
    stream_title: str = "",
) -> Transcript:
    """Refine Whisper transcript words using Gemini 2.5 Flash standard generateContent REST endpoint."""
    if not api_key or not api_key.strip() or not transcript.segments:
        return transcript

    api_key = api_key.strip()
    if not api_key.startswith("AIza"):
        logger.info("Gemini API unavailable/invalid key — using offline Whisper mode.")
        return transcript

    try:
        all_words: list[Word] = [w for seg in transcript.segments for w in seg.words]
        if not all_words:
            return transcript

        words_payload = [{"i": idx, "text": w.text} for idx, w in enumerate(all_words)]

        prompt = (
            "You are an expert audio transcription proofreader and alignment editor.\n"
            "Below is a word-by-word Whisper speech recognition transcript from a video.\n"
            "Whisper frequently makes phonetic errors on names, gamer tags, slang, contractions, and technical jargon.\n\n"
            f"VIDEO TITLE / CONTEXT: {stream_title or 'Video'}\n\n"
            "RAW WORDS (JSON):\n"
            f"{json.dumps(words_payload)}\n\n"
            "INSTRUCTIONS:\n"
            "1. Fix misspelled words, phonetic errors, proper nouns, streamer names, and slang.\n"
            "2. Fix capitalization and contractions (e.g. 'dont' -> 'don\\'t', 'im' -> 'I\\'m').\n"
            "3. CRITICAL: You must preserve the EXACT same number of words and exact same indices 'i'. DO NOT add or drop words. DO NOT output markdown or backslashes.\n"
            "4. Return ONLY a valid JSON array of objects with keys 'i' and 'text'. Example:\n"
            '[{"i": 0, "text": "CorrectedWord"}, ...]\n'
        )

        refined_map: dict[int, str] = {}
        models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash"]

        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
            }
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=12,
                )
                if resp.status_code in (400, 403, 404):
                    logger.info(
                        "Gemini API unavailable/invalid key (HTTP %d) — using offline Whisper mode.",
                        resp.status_code,
                    )
                    return transcript

                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        m_json = re.search(r"\[.*\]", text, re.DOTALL)
                        if m_json:
                            items = json.loads(m_json.group(0))
                            for item in items:
                                if isinstance(item, dict) and "i" in item and "text" in item:
                                    clean_txt = sanitize_gemini_word(item["text"])
                                    if clean_txt:
                                        refined_map[int(item["i"])] = clean_txt
                            if refined_map:
                                break
            except requests.exceptions.RequestException as req_err:
                logger.info(
                    "Gemini API unavailable/request error (%s) — using offline Whisper mode: %s",
                    m,
                    req_err,
                )
                return transcript
            except Exception as e:
                logger.debug("Gemini response parse error (%s): %s", m, e)
                continue

        if refined_map:
            total_whisper_words = len(all_words)
            valid_indices = {i for i in refined_map if 0 <= i < total_whisper_words}
            if len(valid_indices) >= int(total_whisper_words * 0.4):
                logger.info(
                    "Ultra AI (Gemini 2.5 Flash) successfully refined %d/%d words in transcript!",
                    len(valid_indices),
                    total_whisper_words,
                )
                word_idx = 0
                for seg in transcript.segments:
                    seg_words = []
                    for w in seg.words:
                        if word_idx in refined_map:
                            corr = refined_map[word_idx]
                            if corr:
                                w.text = corr
                        else:
                            w.text = sanitize_gemini_word(w.text)
                        seg_words.append(w.text)
                        word_idx += 1
                    seg.text = " ".join(seg_words)
            else:
                logger.warning(
                    "Gemini returned word indices mismatched with Whisper words (%d valid vs %d total). Preserving Whisper words.",
                    len(valid_indices),
                    total_whisper_words,
                )
    except Exception as exc:
        logger.warning(
            "Gemini transcript refinement failed gracefully: %s. Using Whisper transcript.",
            exc,
        )

    return transcript


# ---------------------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------------------


class TranscriptionError(Exception):
    """Raised when transcription fails for any reason."""


class Transcriber:
    """Lazy-loading wrapper around *faster-whisper*.

    The underlying :pyclass:`faster_whisper.WhisperModel` is instantiated on
    first use so that import-time costs stay low.

    Args:
        config: A :class:`TranscriberConfig` instance with model parameters.
    """

    def __init__(self, config: TranscriberConfig) -> None:
        self._config = config
        self._model: Optional[object] = None  # faster_whisper.WhisperModel
        self._loaded_model_size: Optional[str] = None

    # -- model management ----------------------------------------------------

    def _resolve_model_size(self) -> str:
        acc_mode = getattr(self._config, "accuracy_mode", "") or ""
        cfg_size = getattr(self._config, "model_size", "") or ""
        if "medium" in acc_mode.lower() or "medium" in cfg_size.lower():
            return "medium"
        return "small"

    def _load_model(self) -> None:
        """Load the faster-whisper model, auto-detecting the best device.

        CUDA is preferred when available; otherwise falls back to CPU with
        ``int8`` quantisation and 8 CPU threads for maximum throughput and accuracy.

        Raises:
            TranscriptionError: If the model cannot be loaded.
        """
        try:
            import torch
            cuda_available = torch.cuda.is_available()
        except ImportError:
            cuda_available = False

        if cuda_available:
            device = "cuda"
            compute_type = getattr(self._config, "compute_type", "float16")
        else:
            device = "cpu"
            compute_type = "int8"

        model_size = self._resolve_model_size()
        cpu_threads = int(getattr(self._config, "cpu_threads", 8))
        logger.info(
            "Loading faster-whisper model '%s' on %s (%s, cpu_threads=%d) …",
            model_size, device, compute_type, cpu_threads,
        )

        try:
            import faster_whisper
            import faster_whisper.audio

            # Safely patch decode_audio in faster_whisper to avoid PyAV metadata_errors bug
            try:
                def _safe_decode_audio(input_file, *args, **kwargs):
                    if isinstance(input_file, (str, Path)):
                        return _load_audio_waveform(Path(input_file))
                    return np.zeros(0, dtype=np.float32)

                faster_whisper.audio.decode_audio = _safe_decode_audio
            except Exception:
                pass

            self._model = faster_whisper.WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
            )
            self._loaded_model_size = model_size
            logger.info("Model '%s' loaded successfully.", model_size)
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to load faster-whisper model '{model_size}': {exc}"
            ) from exc

    @property
    def model(self) -> object:
        """Return the loaded model, initialising or switching if model size changed."""
        needed_size = self._resolve_model_size()
        if self._model is None or self._loaded_model_size != needed_size:
            self._load_model()
        return self._model

    # -- public API ----------------------------------------------------------

    def transcribe(self, audio_path: Path) -> Transcript:
        """Transcribe an audio or video file.

        If *audio_path* points to a video container the audio track is first
        extracted to a temporary WAV file via *ffmpeg*.

        Args:
            audio_path: Path to an audio or video file.

        Returns:
            A populated :class:`Transcript`.

        Raises:
            FileNotFoundError: If *audio_path* does not exist.
            TranscriptionError: On extraction or recognition failure.
        """
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Input file not found: {audio_path}")

        # If the input is a video, extract audio first.
        if audio_path.suffix.lower() in _VIDEO_EXTENSIONS:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = Path(tmp.name)
            try:
                _extract_audio(audio_path, wav_path)
                return self._run_transcription(wav_path)
            finally:
                wav_path.unlink(missing_ok=True)

        return self._run_transcription(audio_path)

    def transcribe_segment(
        self,
        video_path: Path,
        start: float,
        end: float,
    ) -> Transcript:
        """Extract and transcribe a sub-range of a video's audio.

        Timestamps in the returned :class:`Transcript` are relative to
        *start* (i.e. the segment begins at ``0.0``).

        Args:
            video_path: Path to the source video file.
            start: Start time in seconds.
            end: End time in seconds.

        Returns:
            A :class:`Transcript` covering the requested segment.

        Raises:
            FileNotFoundError: If *video_path* does not exist.
            TranscriptionError: On extraction or recognition failure.
            ValueError: If *end* ≤ *start*.
        """
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")
        if end <= start:
            raise ValueError(
                f"end ({end}) must be greater than start ({start})"
            )

        duration = end - start

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)

        try:
            _extract_audio_segment(video_path, wav_path, start, duration)
            transcript = self._run_transcription(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)

        return transcript

    # -- internal helpers ----------------------------------------------------

    def _run_transcription(self, wav_path: Path) -> Transcript:
        """Run faster-whisper on an audio file and return a :class:`Transcript`.

        Loads audio into a 16kHz mono float32 numpy array with vocal clarity pre-filters,
        utilizes Silero VAD filtering to completely prevent music/silence hallucinations,
        cleans contractions and timing overlaps, and optionally refines words with
        Ultra AI (Gemini 2.5 Flash).

        Args:
            wav_path: Path to an audio/video file.

        Returns:
            Populated :class:`Transcript`.

        Raises:
            TranscriptionError: On any whisper-level failure.
        """
        language = getattr(self._config, "language", None)
        beam_size = getattr(self._config, "beam_size", 5)
        best_of = getattr(self._config, "best_of", 5)
        vad_min_silence = getattr(self._config, "vad_min_silence_duration_ms", 400)

        logger.info("Transcribing '%s' with Silero VAD (min_silence=%dms, beam=%d) …", wav_path.name, vad_min_silence, beam_size)
        t0 = time.perf_counter()

        try:
            audio_array = _load_audio_waveform(wav_path)
            segments_iter, info = self.model.transcribe(  # type: ignore[attr-defined]
                audio_array,
                beam_size=beam_size,
                best_of=best_of,
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=vad_min_silence),
                language=language if language else None,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"faster-whisper failed on '{wav_path}': {exc}"
            ) from exc

        segments: list[Segment] = []
        for seg in segments_iter:
            raw_words: list[Word] = []
            if seg.words:
                for w in seg.words:
                    word_str = getattr(w, "word", getattr(w, "text", ""))
                    raw_words.append(
                        Word(
                            text=str(word_str).strip(),
                            start=round(float(w.start), 3),
                            end=round(float(w.end), 3),
                            confidence=round(float(getattr(w, "probability", getattr(w, "confidence", 0.0))), 4),
                        )
                    )
            # Clean contractions and clamp word timestamps to prevent overlaps
            cleaned_words = clean_and_clamp_words(raw_words)
            if cleaned_words:
                seg_start = cleaned_words[0].start
                seg_end = cleaned_words[-1].end
                seg_text = " ".join(w.text for w in cleaned_words)
            else:
                seg_start = round(seg.start, 3)
                seg_end = round(seg.end, 3)
                seg_text = seg.text.strip()

            if cleaned_words or seg_text:
                segments.append(
                    Segment(
                        text=seg_text,
                        start=seg_start,
                        end=seg_end,
                        words=cleaned_words,
                    )
                )

        elapsed = time.perf_counter() - t0
        logger.info(
            "Transcription complete: %d segment(s), detected language '%s' "
            "(%.1f%%), took %.2fs.",
            len(segments),
            info.language,
            info.language_probability * 100,
            elapsed,
        )

        transcript = Transcript(
            segments=segments,
            language=info.language,
            language_probability=round(info.language_probability, 4),
            duration=round(info.duration, 3),
        )

        # Ultra AI (Gemini 2.5 Flash) refinement if enabled and API key is present
        acc_mode = getattr(self._config, "accuracy_mode", "") or ""
        gemini_key = getattr(self._config, "gemini_api_key", "") or ""
        if "ultra" in acc_mode.lower() and gemini_key:
            stream_title = getattr(self._config, "stream_title", "") or ""
            transcript = refine_transcript_with_gemini(transcript, api_key=gemini_key, stream_title=stream_title)

        return transcript


# ---------------------------------------------------------------------------
# ffmpeg helpers (module-private)
# ---------------------------------------------------------------------------


def _run_ffmpeg(args: list[str]) -> None:
    """Execute an ffmpeg command, raising on failure.

    Args:
        args: Full argument list starting with ``"ffmpeg"``.

    Raises:
        TranscriptionError: If ffmpeg exits with a non-zero code.
    """
    if args and args[0] == "ffmpeg":
        args[0] = _resolve_binary("ffmpeg")
    logger.debug("Running: %s", " ".join(args))
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            **get_subprocess_kwargs(),
        )
    except FileNotFoundError:
        raise TranscriptionError(
            "ffmpeg is not installed or not found on PATH."
        )

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:]
        raise TranscriptionError(
            f"ffmpeg exited with code {result.returncode}: {stderr_tail}"
        )


def _extract_audio(input_path: Path, output_wav: Path) -> None:
    """Extract the full audio track from a media file to 16 kHz mono WAV with vocal clarity filters."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-af", "highpass=f=85,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11",
        str(output_wav),
    ])


def _extract_audio_segment(
    input_path: Path,
    output_wav: Path,
    start: float,
    duration: float,
) -> None:
    """Extract a timed audio segment to 16 kHz mono WAV with vocal clarity filters."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(duration),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-af", "highpass=f=85,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11",
        str(output_wav),
    ])


def _load_audio_waveform(file_path: Path, sample_rate: int = 16000) -> np.ndarray:
    """Load audio from file as a 16kHz mono float32 numpy array with vocal clarity pre-filtering.

    Decodes via ffmpeg raw PCM pipe with vocal bandpass and loudness normalization.
    Completely avoids PyAV container opening, preventing 'metadata_errors'
    API incompatibility issues.
    """
    file_path = Path(file_path)

    # 1. Fast path for standard 16kHz mono 16-bit WAV (already pre-filtered by _extract_audio)
    try:
        with wave.open(str(file_path), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == sample_rate:
                frames = wf.readframes(wf.getnframes())
                return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        pass

    # 2. Universal ffmpeg decode path with vocal bandpass & loudnorm
    cmd = [
        _resolve_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(file_path),
        "-vn",
        "-af", "highpass=f=85,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-f", "s16le",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, **get_subprocess_kwargs())
        if proc.returncode == 0 and proc.stdout:
            return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            stderr_msg = proc.stderr.decode(errors="replace")[-300:] if proc.stderr else "Unknown error"
            logger.warning("ffmpeg decode warning for %s: %s", file_path, stderr_msg)
    except Exception as exc:
        logger.warning("ffmpeg decode failed for %s: %s", file_path, exc)

    # 3. Fallback to scipy or soundfile if present
    try:
        from scipy.io import wavfile
        sr, data = wavfile.read(str(file_path))
        if data.ndim > 1:
            data = data.mean(axis=1)
        if data.dtype == np.int16:
            return data.astype(np.float32) / 32768.0
        return data.astype(np.float32)
    except Exception:
        pass

    raise TranscriptionError(f"Could not decode audio from '{file_path}' into numpy array.")


# ---------------------------------------------------------------------------
# SRT time formatting
# ---------------------------------------------------------------------------


def _seconds_to_srt_time(seconds: float) -> str:
    """Convert a float timestamp to SRT time format ``HH:MM:SS,mmm``.

    Args:
        seconds: Time in seconds (non-negative).

    Returns:
        Formatted SRT timestamp string.
    """
    if seconds < 0:
        seconds = 0.0
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
