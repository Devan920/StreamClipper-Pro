"""Video renderer module for cropping and burning stylish animated captions.

Uses ffmpeg filters and ASS subtitles to produce high-retention vertical,
square, or landscape clips with word-by-word karaoke highlighting.

Key robustness features:
- Silent / no-speech clips: skips libass subtitle burning entirely to prevent crashes.
- Path handling: temporary ASS files are saved in Python's temp directory with sanitized
  alphanumeric filenames and referenced relatively using subprocess cwd to avoid
  Windows drive colon / space parsing bugs in FFmpeg's filtergraph.
- Automatic cleanup of temporary subtitle files.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from config import EditorConfig
    from transcriber import Transcript, Word

from caption_generator import generate_ass_subtitles

logger = logging.getLogger(__name__)

# Target resolution defaults
_DEFAULT_WIDTH = 1080
_DEFAULT_HEIGHT = 1920


def get_subprocess_kwargs() -> dict[str, Any]:
    """Return creationflags and startupinfo to completely suppress terminal pop-ups on Windows."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _ensure_binaries_on_path() -> None:
    """Ensure ffmpeg and ffprobe directories are available in PATH."""
    base_dir = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    candidates = [
        str(base_dir),
        str(Path(sys.executable).parent),
        os.path.expandvars(r"%USERPROFILE%\.spotiflac"),
        r"C:\Program Files\Topaz Labs LLC\Topaz Video AI",
        str(Path(__file__).resolve().parent / "dist" / "StreamClipperPro"),
    ]
    current_path = os.environ.get("PATH", "")
    for p in candidates:
        if os.path.isdir(p) and p not in current_path:
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


def _resolve_binary(name: str) -> str:
    """Resolve an executable binary (e.g. ffmpeg, ffprobe) to a fully qualified path."""
    _ensure_binaries_on_path()
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


class VideoRenderer:
    """Renders video clips with aspect ratio cropping and burned-in ASS captions."""

    def __init__(self, config: EditorConfig) -> None:
        self._config = config
        _ensure_binaries_on_path()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_clip(
        self,
        source_path: Path,
        start: float,
        end: float,
        transcript: Transcript,
        output_name: str | None = None,
    ) -> Path:
        """Extract a clip, apply cropping/face tracking, burn captions, and return output path.

        Parameters
        ----------
        source_path:
            Path to the source video file.
        start:
            Start time in seconds within source video.
        end:
            End time in seconds within source video.
        transcript:
            Transcript containing word-level timestamps.
        output_name:
            Optional stem for output filename. A UUID is used when None.

        Returns
        -------
        Path
            Absolute path to rendered clip.
        """
        source_path = Path(source_path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Source video not found: {source_path}")
        if start >= end:
            raise ValueError(f"start ({start:.3f}s) must be less than end ({end:.3f}s)")

        duration = end - start
        stem = output_name or uuid.uuid4().hex[:12]
        output_dir = Path(self._config.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{stem}.mp4"

        # Safely remove any legacy leftover hidden subtitle files in the output directory
        try:
            for leftover in output_dir.glob(".*_subs.ass"):
                leftover.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info(
            "Creating clip: %s  [%.2f – %.2f]  → %s",
            source_path.name,
            start,
            end,
            output_path.name,
        )

        # Video dimensions
        width, height = self._get_video_dimensions(source_path)
        logger.debug("Source dimensions: %dx%d", width, height)

        # Aspect ratio and face tracking
        target_w = getattr(self._config, "output_width", _DEFAULT_WIDTH)
        target_h = getattr(self._config, "output_height", _DEFAULT_HEIGHT)
        enable_tracking = getattr(self._config, "enable_face_tracking", True)
        layout_mode = getattr(self._config, "layout_mode", "Smart Face Center (Single Frame)")
        facecam_corner = getattr(self._config, "facecam_corner", "Auto-Detect Facecam")

        # --- Gaming Split-Screen shortcut ---
        if layout_mode == "Gaming Split-Screen (Gameplay Top + Facecam Bottom)":
            # Build ASS subtitles first (we still want captions)
            clip_words = []
            if transcript and getattr(transcript, "segments", None):
                for seg in transcript.segments:
                    for w in seg.words:
                        if w.end > start and w.start < end:
                            clip_words.append(w)

            ass_path_for_split: Path | None = None
            sub_temp_dir = Path(tempfile.gettempdir()) / "streamclipper_subs"
            sub_temp_dir.mkdir(parents=True, exist_ok=True)

            if clip_words:
                ass_content = generate_ass_subtitles(clip_words, self._config, start_offset=start)
                if ass_content.strip():
                    sub_filename = f"gss_subs_{uuid.uuid4().hex[:10]}.ass"
                    ass_path_for_split = sub_temp_dir / sub_filename
                    ass_path_for_split.write_text(ass_content, encoding="utf-8")
            else:
                logger.info("Gaming Split-Screen: no spoken words; rendering without subtitles.")

            try:
                from face_tracker import detect_facecam_region
                fc_x, fc_y, fc_w, fc_h = detect_facecam_region(source_path, facecam_corner)
            except Exception as exc:
                logger.warning("detect_facecam_region failed (%s); using Bottom-Right default.", exc)
                fc_x, fc_y, fc_w, fc_h = 1370, 600, 550, 480

            try:
                return render_gaming_split_screen(
                    input_path=source_path,
                    output_path=output_path,
                    facecam_x=fc_x,
                    facecam_y=fc_y,
                    facecam_w=fc_w,
                    facecam_h=fc_h,
                    crf=getattr(self._config, "crf", 18),
                    preset=getattr(self._config, "preset", "medium"),
                    audio_bitrate=getattr(self._config, "audio_bitrate", "192k"),
                    start=start,
                    duration=duration,
                    ass_path=ass_path_for_split,
                )
            finally:
                if ass_path_for_split and ass_path_for_split.is_file():
                    try:
                        ass_path_for_split.unlink(missing_ok=True)
                    except OSError:
                        pass

        # --- Standard single-frame face-tracking pipeline ---
        crop_filter = None
        if enable_tracking:
            try:
                from face_tracker import FaceTracker
                tracker = FaceTracker()
                track_res = tracker.analyze_clip(
                    video_path=source_path,
                    start_time=start,
                    duration=duration,
                    target_w=target_w,
                    target_h=target_h,
                )
                crop_filter = track_res.crop_filter
                if track_res.has_face:
                    logger.info(
                        "Face tracking: %s framing applied",
                        "dynamic pan" if track_res.is_dynamic else "static speaker",
                    )
            except Exception as exc:
                logger.warning(
                    "Face tracking analysis failed (%s); using center-crop fallback.",
                    exc,
                )

        if not crop_filter:
            crop_filter = self._build_crop_filter(width, height)

        # Collect words falling within this clip window
        clip_words = []
        if transcript and getattr(transcript, "segments", None):
            for seg in transcript.segments:
                for w in seg.words:
                    if w.end > start and w.start < end:
                        clip_words.append(w)

        # Setup temp subtitle directory outside output directory
        sub_temp_dir = Path(tempfile.gettempdir()) / "streamclipper_subs"
        sub_temp_dir.mkdir(parents=True, exist_ok=True)

        # Periodic cleanup of old temp subs (> 1 hour old)
        try:
            now = time.time()
            for old_file in sub_temp_dir.glob("subs_*.ass"):
                if now - old_file.stat().st_mtime > 3600:
                    old_file.unlink(missing_ok=True)
        except Exception:
            pass

        ass_tmp: Path | None = None
        caption_filter = ""

        # Handle silent / no-speech clips gracefully
        if not clip_words:
            logger.info(
                "No spoken words detected in clip [%.2f – %.2f]; rendering without subtitle filter.",
                start,
                end,
            )
        else:
            ass_content = generate_ass_subtitles(clip_words, self._config, start_offset=start)
            if ass_content.strip():
                # Write to tempfile.gettempdir() with clean alphanumeric name
                sub_filename = f"subs_{uuid.uuid4().hex[:12]}.ass"
                ass_tmp = sub_temp_dir / sub_filename
                ass_tmp.write_text(ass_content, encoding="utf-8")
                logger.debug("Wrote ASS subtitles to temp: %s", ass_tmp)
                # By running ffmpeg with cwd=sub_temp_dir, we can reference just the filename,
                # completely bypassing Windows colons, backslashes, and folder spaces.
                caption_filter = f"ass='{sub_filename}'"

        try:
            cmd = self._build_ffmpeg_command(
                source=source_path,
                output=output_path,
                start=start,
                duration=duration,
                crop_filter=crop_filter,
                caption_filter=caption_filter,
            )

            logger.debug("ffmpeg command: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                cwd=str(sub_temp_dir),
                capture_output=True,
                text=True,
                check=False,
                **get_subprocess_kwargs(),
            )

            if result.returncode != 0:
                stderr_text = result.stderr or ""
                lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
                err_summary = "\n".join(lines[-10:]) if lines else "Unknown FFmpeg error"
                logger.error("FFmpeg failed (code %d):\n%s", result.returncode, err_summary)
                raise RuntimeError(
                    f"FFmpeg exited with code {result.returncode}:\n{err_summary}"
                )

            logger.info("Clip successfully rendered to %s", output_path)
            return output_path

        finally:
            # Clean up temp ASS file
            if ass_tmp and ass_tmp.is_file():
                try:
                    ass_tmp.unlink()
                    logger.debug("Cleaned up temp ASS file %s", ass_tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_video_dimensions(self, path: Path) -> Tuple[int, int]:
        """Return (width, height) of the first video stream via ffprobe."""
        ffprobe_bin = _resolve_binary("ffprobe")
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path.resolve()),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs()
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffprobe failed for {path}: {exc.stderr}") from exc

        try:
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            return int(stream["width"]), int(stream["height"])
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise RuntimeError(
                f"Unexpected ffprobe output for {path}: {result.stdout}"
            ) from exc

    def _build_crop_filter(self, input_width: int, input_height: int) -> str:
        """Return an ffmpeg crop+scale filter string matching target aspect ratio."""
        target_w = getattr(self._config, "output_width", _DEFAULT_WIDTH)
        target_h = getattr(self._config, "output_height", _DEFAULT_HEIGHT)

        in_aspect = input_width / max(1, input_height)
        out_aspect = target_w / max(1, target_h)

        if abs(in_aspect - out_aspect) < 0.02:
            return f"scale={target_w}:{target_h},setsar=1"
        elif in_aspect > out_aspect:
            # Source is wider than target -> crop sides
            return (
                f"crop=ih*{target_w}/{target_h}:ih:(iw-ih*{target_w}/{target_h})/2:0,"
                f"scale={target_w}:{target_h},setsar=1"
            )
        else:
            # Source is taller than target -> crop top/bottom
            return (
                f"crop=iw:iw*{target_h}/{target_w}:0:(ih-iw*{target_h}/{target_w})/2,"
                f"scale={target_w}:{target_h},setsar=1"
            )

    def _build_ffmpeg_command(
        self,
        source: Path,
        output: Path,
        start: float,
        duration: float,
        crop_filter: str,
        caption_filter: str,
    ) -> list[str]:
        """Assemble the full ffmpeg command arguments list."""
        ffmpeg_bin = _resolve_binary("ffmpeg")
        vf = f"{crop_filter},{caption_filter}" if caption_filter else crop_filter

        crf = str(getattr(self._config, "crf", 17))
        preset = str(getattr(self._config, "preset", "medium"))
        audio_bitrate = str(getattr(self._config, "audio_bitrate", "192k"))

        return [
            ffmpeg_bin,
            "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source.resolve()),
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            str(output.resolve()),
        ]


def concat_segments(
    segment_paths: list[Path],
    output_path: Path,
    max_duration: float | None = None,
) -> Path:
    """Concatenate multiple video segments into a single file via ffmpeg concat demuxer (lossless -c copy)."""
    if not segment_paths:
        raise ValueError("No segment paths provided to concatenate.")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(segment_paths) == 1 and segment_paths[0].suffix.lower() == output_path.suffix.lower():
        shutil.copy2(segment_paths[0], output_path)
        return output_path

    # Create temporary concat list file
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        list_file = Path(f.name)
        for sp in segment_paths:
            resolved = Path(sp).resolve()
            clean_str = str(resolved).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{clean_str}'\n")

    cmd = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file.resolve()),
        "-c", "copy",
    ]
    if output_path.suffix.lower() == ".mp4":
        cmd.extend(["-movflags", "+faststart"])
    if max_duration and max_duration > 0:
        cmd.extend(["-t", f"{max_duration:.3f}"])
    cmd.append(str(output_path))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            **get_subprocess_kwargs(),
        )
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "")[-300:]
            logger.warning("concat_segments failed (code %d): %s; fallback copy", proc.returncode, stderr_tail)
            shutil.copy2(segment_paths[-1], output_path)
    finally:
        list_file.unlink(missing_ok=True)

    return output_path



def render_gaming_split_screen(
    input_path: "Path",
    output_path: "Path",
    facecam_x: int,
    facecam_y: int,
    facecam_w: int,
    facecam_h: int,
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    start: float = 0.0,
    duration: float | None = None,
    ass_path: "Path | None" = None,
) -> "Path":
    """Render a 9:16 Gaming Split-Screen clip (1080×1920).

    Top half (1080×1180): main gameplay cropped from full frame.
    Bottom half (1080×740): facecam crop+scale from detected corner.
    4-pixel dark separator between halves.
    Optional ASS subtitle overlay burned in.

    Parameters
    ----------
    input_path:
        Path to the source video file.
    output_path:
        Desired output file path (.mp4).
    facecam_x, facecam_y, facecam_w, facecam_h:
        Facecam crop region in source pixel coordinates.
    crf:
        x264 CRF quality (default 18).
    preset:
        x264 preset (default "medium").
    audio_bitrate:
        AAC audio bitrate (default "192k").
    start:
        Start time offset in seconds.
    duration:
        Clip duration in seconds (None = full file).
    ass_path:
        Optional path to an ASS subtitle file to burn in.

    Returns
    -------
    Path
        Absolute path to the rendered output file.
    """
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = _resolve_binary("ffmpeg")

    # Build filter_complex:
    # - [top]: full frame crop to 1080-wide by 1180-tall (gameplay), + 4px black pad at bottom
    # - [bottom]: facecam crop region, scale to 1080×736, + 4px black pad at top
    # - vstack both halves to produce 1080×1920
    fc_x = max(0, facecam_x)
    fc_y = max(0, facecam_y)
    fc_w = max(2, facecam_w - (facecam_w % 2))
    fc_h = max(2, facecam_h - (facecam_h % 2))

    filter_complex = (
        f"[0:v]crop=1920:1080:0:0,scale=1080:1180,pad=1080:1184:0:0:color=black[top];"
        f"[0:v]crop={fc_w}:{fc_h}:{fc_x}:{fc_y},scale=1080:736,pad=1080:740:0:4:color=black[bot];"
        f"[top][bot]vstack=inputs=2[stacked]"
    )

    # Determine final video filter (with or without subtitles)
    final_vf_node = "[stacked]"
    sub_filter_part = ""
    ass_tmp_name: str | None = None
    sub_tmp_dir: Path | None = None

    if ass_path is not None and Path(ass_path).is_file() and Path(ass_path).stat().st_size > 10:
        # Use cwd trick: copy/symlink to temp dir and reference by basename to avoid Windows path issues
        sub_tmp_dir = Path(tempfile.gettempdir()) / "streamclipper_subs"
        sub_tmp_dir.mkdir(parents=True, exist_ok=True)
        ass_tmp_name = f"gss_{uuid.uuid4().hex[:10]}.ass"
        import shutil as _shutil
        _shutil.copy2(str(ass_path), str(sub_tmp_dir / ass_tmp_name))
        # Append subtitle filter to filter_complex chain
        filter_complex += f";[stacked]ass='{ass_tmp_name}'[out]"
        final_vf_node = "[out]"
    else:
        filter_complex += f";[stacked]copy[out]"
        final_vf_node = "[out]"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
    ]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", final_vf_node,
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        str(output_path),
    ]

    cwd = str(sub_tmp_dir) if sub_tmp_dir else str(output_path.parent)
    logger.debug("render_gaming_split_screen ffmpeg cmd: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            **get_subprocess_kwargs(),
        )
        if result.returncode != 0:
            stderr_text = result.stderr or ""
            lines = [l.strip() for l in stderr_text.splitlines() if l.strip()]
            err_summary = "\n".join(lines[-10:]) if lines else "Unknown FFmpeg error"
            logger.error("render_gaming_split_screen FFmpeg failed (code %d):\n%s", result.returncode, err_summary)
            raise RuntimeError(
                f"render_gaming_split_screen: FFmpeg exited {result.returncode}:\n{err_summary}"
            )
        logger.info("Gaming split-screen clip rendered to %s", output_path)
        return output_path
    finally:
        # Clean up temp ASS copy
        if sub_tmp_dir and ass_tmp_name:
            tmp_ass = sub_tmp_dir / ass_tmp_name
            try:
                tmp_ass.unlink(missing_ok=True)
            except OSError:
                pass


# Backward-compatible aliases
VideoEditor = VideoRenderer
resolve_binary = _resolve_binary
