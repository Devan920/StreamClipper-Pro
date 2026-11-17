"""Video editor module (backwards compatibility wrapper).

Re-exports VideoRenderer as VideoEditor and caption functions from
caption_generator and video_renderer.
"""

from __future__ import annotations

from caption_generator import (
    CaptionGenerator,
    build_ass_header,
    contains_indic as _contains_indic,
    escape_ass_text as _escape_ass_text,
    format_ass_time as _format_ass_time,
    format_word_display as _format_word_display,
    generate_ass_subtitles,
    hex_to_ass_color as _hex_to_ass_color,
)
from video_renderer import (
    VideoEditor,
    VideoRenderer,
    concat_segments,
    _ensure_binaries_on_path,
    _resolve_binary,
)

__all__ = [
    "VideoEditor",
    "VideoRenderer",
    "concat_segments",
    "CaptionGenerator",
    "generate_ass_subtitles",
    "_format_ass_time",
    "_escape_ass_text",
    "_contains_indic",
    "_hex_to_ass_color",
    "_format_word_display",
    "_resolve_binary",
]
