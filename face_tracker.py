"""Auto Face Tracking module for Stream Clipper Pro.

Uses OpenCV's YuNet DNN model to detect primary speakers, applies temporal
smoothing (EMA + deadzone threshold) to prevent jitter, and generates
smooth cinematic cropping for 9:16, 4:5, and 1:1 aspect ratios.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class FaceTrackResult:
    """Result of analyzing face movements in a video segment."""

    has_face: bool
    crop_w: int
    crop_h: int
    crop_filter: str
    is_dynamic: bool
    mean_confidence: float = 0.0


def _resolve_model_path() -> Path:
    """Locate the YuNet ONNX model path across dev, installed, and frozen environments."""
    candidates: list[Path] = []

    # 1. PyInstaller _MEIPASS
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets" / "models" / "face_detection_yunet.onnx")
        app_dir = Path(sys.executable).resolve().parent
        candidates.append(app_dir / "assets" / "models" / "face_detection_yunet.onnx")

    # 2. Local module directory
    here = Path(__file__).resolve().parent
    candidates.append(here / "assets" / "models" / "face_detection_yunet.onnx")
    candidates.append(here.parent / "assets" / "models" / "face_detection_yunet.onnx")

    for p in candidates:
        if p.is_file() and p.stat().st_size > 10000:
            return p

    # Default location to create/download if missing
    default_path = here / "assets" / "models" / "face_detection_yunet.onnx"
    default_path.parent.mkdir(parents=True, exist_ok=True)
    return default_path


class FaceTracker:
    """Detects and tracks faces in video clips to generate smooth crop windows."""

    def __init__(self, model_path: Path | str | None = None, min_score: float = 0.5) -> None:
        self.min_score = min_score
        self.model_path = Path(model_path) if model_path else _resolve_model_path()
        self._detector: Any = None
        self._init_detector()

    def _init_detector(self) -> None:
        """Initialize OpenCV YuNet FaceDetectorYN."""
        try:
            import cv2

            if not self.model_path.is_file() or self.model_path.stat().st_size < 10000:
                self._download_model()

            if self.model_path.is_file() and hasattr(cv2, "FaceDetectorYN"):
                self._detector = cv2.FaceDetectorYN.create(
                    str(self.model_path),
                    "",
                    (320, 320),
                    score_threshold=self.min_score,
                    nms_threshold=0.3,
                    top_k=5,
                )
                logger.info("YuNet face detector initialized from %s", self.model_path.name)
            else:
                logger.warning("cv2.FaceDetectorYN unavailable or model missing. Face tracking disabled.")
        except Exception as exc:
            logger.warning("Failed to initialize FaceDetectorYN: %s. Using center crop fallback.", exc)
            self._detector = None

    def _download_model(self) -> None:
        """Download official YuNet model if not present."""
        try:
            import requests

            url = (
                "https://github.com/opencv/opencv_zoo/raw/main/models/"
                "face_detection_yunet/face_detection_yunet_2023mar.onnx"
            )
            logger.info("Downloading YuNet model to %s ...", self.model_path)
            r = requests.get(url, timeout=15, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100000:
                self.model_path.parent.mkdir(parents=True, exist_ok=True)
                self.model_path.write_bytes(r.content)
                logger.info("YuNet model downloaded successfully (%d bytes)", len(r.content))
        except Exception as exc:
            logger.warning("Could not download YuNet model: %s", exc)

    def analyze_clip(
        self,
        video_path: Path | str,
        start_time: float,
        duration: float,
        target_w: int,
        target_h: int,
        sample_fps: float = 6.0,
        deadzone_px: float = 40.0,
        ema_alpha: float = 0.12,
    ) -> FaceTrackResult:
        """Analyze a clip segment to determine optimal crop coordinates or smooth panning.

        Parameters
        ----------
        video_path : Path | str
            Source video file.
        start_time : float
            Start offset in seconds.
        duration : float
            Duration in seconds.
        target_w : int
            Target output width (e.g. 1080).
        target_h : int
            Target output height (e.g. 1920).
        sample_fps : float
            Frames per second to sample for face detection (default: 6.0).
        deadzone_px : float
            Pixel deadband to prevent micro-jitter when head movements are small.
        ema_alpha : float
            Exponential moving average smoothing factor (0.05-0.20).

        Returns
        -------
        FaceTrackResult
            Contains the recommended ffmpeg crop filter string.
        """
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning("Could not open %s for face tracking; using center crop", video_path)
            return self._fallback_center_crop(1920, 1080, target_w, target_h)

        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0

        in_aspect = orig_w / max(1, orig_h)
        out_aspect = target_w / max(1, target_h)

        # If source already matches target aspect (e.g. 16:9 to 16:9), no horizontal crop
        if abs(in_aspect - out_aspect) < 0.02 or in_aspect <= out_aspect:
            cap.release()
            return self._fallback_center_crop(orig_w, orig_h, target_w, target_h)

        # Source is wider than target -> horizontal crop needed
        crop_h = orig_h
        crop_w = int(orig_h * target_w / target_h)
        crop_w = crop_w - (crop_w % 2)  # Ensure even width for x264
        max_x = max(0, orig_w - crop_w)
        center_x = max_x / 2.0

        if self._detector is None:
            cap.release()
            return FaceTrackResult(
                has_face=False,
                crop_w=crop_w,
                crop_h=crop_h,
                crop_filter=f"crop={crop_w}:{crop_h}:{int(center_x)}:0,scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
            )

        # Detector input scaling (e.g. downscale to ~320x180 for super-fast inference)
        det_w = 320
        det_h = int(det_w * orig_h / orig_w)
        det_h = det_h - (det_h % 2)
        scale_x = orig_w / float(det_w)
        self._detector.setInputSize((det_w, det_h))

        # Sample frames across [start_time, start_time + duration]
        frame_step = max(1, int(round(fps / sample_fps)))
        start_frame = max(0, int(start_time * fps))
        total_sample_frames = max(1, int(duration * fps))

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        samples: list[tuple[float, float | None, float]] = []  # (rel_time, face_center_x, score)
        frames_read = 0

        while frames_read < total_sample_frames:
            ret, frame = cap.read()
            if not ret or frame is None:
                break

            if frames_read % frame_step == 0:
                rel_t = frames_read / fps
                small = cv2.resize(frame, (det_w, det_h), interpolation=cv2.INTER_LINEAR)
                _, faces = self._detector.detect(small)

                if faces is not None and len(faces) > 0:
                    # Pick primary speaker face (score * area)
                    best_face = None
                    best_metric = -1.0
                    for face in faces:
                        fx, fy, fw, fh = face[0:4]
                        score = float(face[14])
                        metric = (fw * fh) * (0.5 + score)
                        if metric > best_metric:
                            best_metric = metric
                            best_face = (fx, fy, fw, fh, score)

                    if best_face:
                        fx, fy, fw, fh, score = best_face
                        face_cx = (fx + fw / 2.0) * scale_x
                        samples.append((rel_t, face_cx, score))
                    else:
                        samples.append((rel_t, None, 0.0))
                else:
                    samples.append((rel_t, None, 0.0))

            frames_read += 1

        cap.release()

        # Check if enough faces were found
        valid_samples = [s for s in samples if s[1] is not None]
        if len(valid_samples) < max(2, int(len(samples) * 0.15)):
            logger.info("Face tracking: no dominant face detected (found %d/%d). Using center crop.",
                        len(valid_samples), len(samples))
            return FaceTrackResult(
                has_face=False,
                crop_w=crop_w,
                crop_h=crop_h,
                crop_filter=f"crop={crop_w}:{crop_h}:{int(center_x)}:0,scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
            )

        # Interpolate gaps in raw face detections
        filled_x: list[tuple[float, float]] = []
        last_known_x = valid_samples[0][1]
        for t, x, _ in samples:
            if x is not None:
                last_known_x = x
                filled_x.append((t, x))
            else:
                filled_x.append((t, last_known_x if last_known_x is not None else (orig_w / 2.0)))

        # Convert face_center_x to desired crop_x (clamped)
        raw_crop_x: list[tuple[float, float]] = []
        for t, fc_x in filled_x:
            target_cx = fc_x - (crop_w / 2.0)
            target_cx = max(0.0, min(float(max_x), target_cx))
            raw_crop_x.append((t, target_cx))

        # Apply Deadzone + EMA Temporal Smoothing
        smoothed_x: list[tuple[float, float]] = []
        curr_x = raw_crop_x[0][1]
        for t, target_cx in raw_crop_x:
            diff = target_cx - curr_x
            if abs(diff) > deadzone_px:
                # Outside deadzone: pan smoothly
                step = diff - (deadzone_px if diff > 0 else -deadzone_px)
                curr_x += ema_alpha * step
                curr_x = max(0.0, min(float(max_x), curr_x))
            smoothed_x.append((t, curr_x))

        x_values = [x for _, x in smoothed_x]
        x_range = max(x_values) - min(x_values)
        avg_score = float(np.mean([s[2] for s in valid_samples]))

        # Case A: Static or tightly localized face (range < 70px)
        # Using a fixed crop avoids subtle panning artifacts
        if x_range < 70.0:
            median_x = int(round(float(np.median(x_values))))
            median_x = max(0, min(max_x, median_x))
            logger.info("Face tracking: stable speaker detected (x=%d, conf=%.2f). Static crop used.",
                        median_x, avg_score)
            return FaceTrackResult(
                has_face=True,
                crop_w=crop_w,
                crop_h=crop_h,
                crop_filter=f"crop={crop_w}:{crop_h}:{median_x}:0,scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
                mean_confidence=avg_score,
            )

        # Case B: Dynamic movement across the screen -> generate smooth FFmpeg lerp expression
        # Subsample to keyframes spaced ~1.0s apart
        keyframes = self._extract_keyframes(smoothed_x, min_dt=1.0, max_keyframes=24)
        expr = self._build_ffmpeg_lerp_expression(keyframes, max_x=max_x)

        logger.info("Face tracking: dynamic movement tracked (range=%.0fpx, %d keyframes, conf=%.2f). Smooth pan enabled.",
                    x_range, len(keyframes), avg_score)

        crop_filter = f"crop={crop_w}:{crop_h}:{expr}:y=0,scale={target_w}:{target_h},setsar=1"
        return FaceTrackResult(
            has_face=True,
            crop_w=crop_w,
            crop_h=crop_h,
            crop_filter=crop_filter,
            is_dynamic=True,
            mean_confidence=avg_score,
        )

    def _fallback_center_crop(
        self, orig_w: int, orig_h: int, target_w: int, target_h: int
    ) -> FaceTrackResult:
        """Standard center-crop fallback."""
        in_aspect = orig_w / max(1, orig_h)
        out_aspect = target_w / max(1, target_h)

        if abs(in_aspect - out_aspect) < 0.02:
            return FaceTrackResult(
                has_face=False,
                crop_w=orig_w,
                crop_h=orig_h,
                crop_filter=f"scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
            )
        elif in_aspect > out_aspect:
            crop_h = orig_h
            crop_w = int(orig_h * target_w / target_h)
            crop_w = crop_w - (crop_w % 2)
            crop_x = int((orig_w - crop_w) / 2)
            return FaceTrackResult(
                has_face=False,
                crop_w=crop_w,
                crop_h=crop_h,
                crop_filter=f"crop={crop_w}:{crop_h}:{crop_x}:0,scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
            )
        else:
            crop_w = orig_w
            crop_h = int(orig_w * target_h / target_w)
            crop_h = crop_h - (crop_h % 2)
            crop_y = int((orig_h - crop_h) / 2)
            return FaceTrackResult(
                has_face=False,
                crop_w=crop_w,
                crop_h=crop_h,
                crop_filter=f"crop={crop_w}:{crop_h}:0:{crop_y},scale={target_w}:{target_h},setsar=1",
                is_dynamic=False,
            )

    @staticmethod
    def _extract_keyframes(
        trajectory: list[tuple[float, float]], min_dt: float = 1.0, max_keyframes: int = 24
    ) -> list[tuple[float, float]]:
        """Downsample smoothed trajectory to keyframe timestamps."""
        if not trajectory:
            return [(0.0, 0.0)]

        keyframes: list[tuple[float, float]] = [trajectory[0]]
        last_t = trajectory[0][0]

        for t, x in trajectory[1:]:
            if (t - last_t) >= min_dt:
                keyframes.append((round(t, 2), round(x, 1)))
                last_t = t

        # Ensure final frame is included
        final_pt = (round(trajectory[-1][0], 2), round(trajectory[-1][1], 1))
        if keyframes[-1][0] < final_pt[0]:
            keyframes.append(final_pt)

        # Cap keyframes if video is long to prevent FFmpeg expression stack limits
        if len(keyframes) > max_keyframes:
            step = len(keyframes) / float(max_keyframes)
            keyframes = [keyframes[int(i * step)] for i in range(max_keyframes - 1)] + [keyframes[-1]]

        return keyframes

    @staticmethod
    def _build_ffmpeg_lerp_expression(
        keyframes: list[tuple[float, float]], max_x: int
    ) -> str:
        """Build nested if(lt(t, t_next), lerp(...), ...) FFmpeg expression for continuous pan."""
        if len(keyframes) <= 1:
            return f"clip({int(keyframes[0][1])}\\,0\\,{max_x})"

        # Start from the last segment and nest backwards
        last_t, last_x = keyframes[-1]
        expr = f"{int(last_x)}"

        for i in range(len(keyframes) - 2, -1, -1):
            t0, x0 = keyframes[i]
            t1, x1 = keyframes[i + 1]
            dt = max(0.01, t1 - t0)
            # lerp(x0, x1, (t-t0)/(t1-t0)) in FFmpeg syntax
            segment = f"lerp({int(x0)}\\,{int(x1)}\\,(t-{t0:.2f})/{dt:.2f})"
            expr = f"if(lt(t\\,{t1:.2f})\\,{segment}\\,{expr})"

        return f"clip({expr}\\,0\\,{max_x})"
