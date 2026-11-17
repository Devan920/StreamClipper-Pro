"""Caption generation module for producing viral ASS subtitles with karaoke animations.

Supports 8 distinct viral subtitle presets:
1. Hormozi Pop (Classic) — Bold uppercase white text, bright yellow highlight, 115% pop, heavy stroke
2. MrBeast Viral (Green) — Extra-heavy uppercase text, bright neon green highlight, 122% aggressive pop
3. Cyberpunk Neon (Gaming) — Electric cyan text, magenta blur glow border, bright white highlight
4. Fire Hype (Red/Orange) — White text, vibrant fire orange-red highlight, bounce pop, thick dark outline
5. Podcast Box (Clean) — Crisp white text in sleek dark semi-transparent box, amber highlight
6. Gold Luxury (Aesthetic) — Rich metallic gold text, pure white highlight, soft drop shadow
7. Minimalist Clean — Sleek medium-weight white text, subtle soft shadow, no heavy borders
8. Karaoke Sweep — Smooth left-to-right color fill sweep using ASS \\kf tags

Maintains:
- 2–3 words per chunk for high-energy styles
- 4–5 words per chunk for cleaner/sweep styles
- PlayResX / PlayResY scaling across 9:16, 16:9, 1:1, 4:5
- Full Malayalam / Indic Unicode support using Nirmala UI
- Graceful handling of silent / no-speech clips
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Sequence, Tuple

if TYPE_CHECKING:
    from config import EditorConfig
    from transcriber import Word

logger = logging.getLogger(__name__)

# Default target dimensions
_DEFAULT_WIDTH = 1080
_DEFAULT_HEIGHT = 1920

# Style preset catalog
CAPTION_STYLES = [
    "Viral Glow Pop (Shorts Edit)",
    "Hormozi Pop (Classic)",
    "MrBeast Viral (Green)",
    "Cyberpunk Neon (Gaming)",
    "Fire Hype (Red/Orange)",
    "Podcast Box (Clean)",
    "Gold Luxury (Aesthetic)",
    "Minimalist Clean",
    "Karaoke Sweep",
]

# Color constants for Viral Glow Pop (ASS &HAABBGGRR)
_NEON_GREEN = "&H0014FF39"
_VIBRANT_RED = "&H001E1EFF"
_ELECTRIC_CYAN = "&H00FFFF00"
_BRIGHT_YELLOW = "&H0000D7FF"
_PURE_WHITE = "&H00FFFFFF"

_VIRAL_COLOR_CYCLE = [_NEON_GREEN, _BRIGHT_YELLOW, _ELECTRIC_CYAN, _VIBRANT_RED]

_HIGH_IMPACT_KEYWORDS = frozenset({
    "INSANE", "CRAZY", "WIN", "OMG", "WTF", "KILL", "DEAD", "WOW",
    "YES", "NO", "HUGE", "NEVER", "ALWAYS", "SECRET", "STOP", "BEST",
    "WORST", "FAIL", "CHALLENGE", "RICH", "MONEY", "BROKE", "FREE",
    "WARNING", "DANGER", "EPIC", "UNBELIEVABLE", "CLUTCH", "HACK",
    "WHAT", "WHY", "NOW", "GO", "FAST", "LOCK",
})


def _get_viral_word_color(word_text: str, index: int) -> str:
    """Return vibrant highlight color based on keyword impact or rotation cycle."""
    upper = word_text.upper().strip()
    # Check for currency or numbers/percentages
    if any(c in upper for c in ("$", "€", "£", "%")) or re.search(r"\d", upper):
        if any(w in upper for w in ("-", "LOST", "DOWN", "FAIL", "BROKE")):
            return _VIBRANT_RED
        return _NEON_GREEN
    # Check high-impact keywords
    clean_keyword = re.sub(r"[^A-Z]", "", upper)
    if clean_keyword in _HIGH_IMPACT_KEYWORDS:
        if clean_keyword in ("FAIL", "DEAD", "KILL", "STOP", "NO", "DANGER", "WARNING", "WORST"):
            return _VIBRANT_RED
        return _NEON_GREEN
    # Alternating rotation cycle across words/phrases
    return _VIRAL_COLOR_CYCLE[index % len(_VIRAL_COLOR_CYCLE)]


def format_ass_time(seconds: float) -> str:
    """Convert *seconds* to ASS timestamp format ``H:MM:SS.cc``."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def sanitize_caption_text(text: str) -> str:
    """Strip all markdown, newlines, control characters, backslashes, and curly braces from subtitle text."""
    if not text:
        return ""
    # Strip markdown symbols (*, `, #, ~, _, <, >, {, }, [, ], \)
    t = re.sub(r"[\*`#~_<>{}\[\]\\]", "", str(text))
    t = t.replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip("\"'")


def escape_ass_text(text: str) -> str:
    """Escape special characters for ASS dialogue lines."""
    clean = sanitize_caption_text(text)
    return clean.replace("{", "(").replace("}", ")").replace("\\", "")


def contains_indic(text: str) -> bool:
    """Check if text contains Indic (Malayalam, Tamil, Hindi, etc.) characters."""
    for ch in text:
        if "\u0900" <= ch <= "\u0D7F":
            return True
    return False


def hex_to_ass_color(color: str, default_alpha: str = "00") -> str:
    """Convert hex (#RRGGBB, #AARRGGBB, or &HAABBGGRR) to valid ASS &HAABBGGRR&."""
    if not color:
        return f"&H{default_alpha}FFFFFF&"
    c = color.strip().lstrip("#").lstrip("&H").rstrip("&")
    if len(c) == 6:
        r, g, b = c[0:2], c[2:4], c[4:6]
        return f"&H{default_alpha}{b}{g}{r}&"
    elif len(c) == 8:
        if c.startswith(("00", "80", "A0", "CC", "FF")):
            a, b, g, r = c[0:2], c[2:4], c[4:6], c[6:8]
            return f"&H{a}{b}{g}{r}&"
        r, g, b, a = c[0:2], c[2:4], c[4:6], c[6:8]
        return f"&H{a}{b}{g}{r}&"
    return f"&H{default_alpha}FFFFFF&"


def format_word_display(text: str, is_indic: bool) -> str:
    """Format word text: sanitize markdown/control chars, uppercase English/Latin words, preserve Indic Unicode."""
    cleaned = sanitize_caption_text(text)
    if not is_indic:
        cleaned = cleaned.upper()
    return escape_ass_text(cleaned)


def build_ass_header(
    config: EditorConfig | None = None,
    font_name: str | None = None,
    font_size: int | None = None,
    primary_color: str = "&H00FFFFFF&",
    secondary_color: str = "&H0000D7FF&",
    outline_color: str = "&H00000000&",
    back_color: str = "&H00000000&",
    border_style: int = 1,
    outline: float = 5.0,
    shadow: float = 3.0,
    margin_v: int = 380,
    target_w: int = _DEFAULT_WIDTH,
    target_h: int = _DEFAULT_HEIGHT,
) -> str:
    """Build a complete, standard ASS header section."""
    fn = font_name or (getattr(config, "font_name", "Arial Black") if config else "Arial Black")
    fs = font_size or (int(getattr(config, "font_size", 78)) if config else 78)

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "Title: Stream Clipper Viral Captions\n"
        f"PlayResX: {target_w}\n"
        f"PlayResY: {target_h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{fn},{fs},"
        f"{primary_color},{secondary_color},"
        f"{outline_color},{back_color},"
        f"-1,0,0,0,"  # Bold=-1
        f"100,100,0,0,"
        f"{border_style},{outline},{shadow},"
        f"2,"  # Alignment: bottom-centre
        f"40,40,{margin_v},"
        f"1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


def _resolve_theme(theme_raw: str) -> str:
    """Normalize theme string to canonical name, handling legacy settings."""
    t = (theme_raw or "").strip().lower()
    if "viral glow" in t or "shorts edit" in t:
        return "Viral Glow Pop (Shorts Edit)"
    if "mrbeast" in t or "green" in t:
        return "MrBeast Viral (Green)"
    if "cyber" in t or "neon" in t or "glow" in t:
        return "Cyberpunk Neon (Gaming)"
    if "fire" in t or "red" in t or "orange" in t:
        return "Fire Hype (Red/Orange)"
    if "podcast" in t or "box" in t or "clean white" in t:
        return "Podcast Box (Clean)"
    if "gold" in t or "luxury" in t or "aesthetic" in t:
        return "Gold Luxury (Aesthetic)"
    if "minimal" in t:
        return "Minimalist Clean"
    if "karaoke" in t or "sweep" in t:
        return "Karaoke Sweep"
    if "hormozi" in t or "classic" in t:
        return "Hormozi Pop (Classic)"
    return "Viral Glow Pop (Shorts Edit)"


def generate_ass_subtitles(
    words: Sequence[Word],
    config: EditorConfig,
    start_offset: float = 0,
) -> str:
    """Generate high-retention viral ASS subtitles with word-by-word pop animations.

    Supports 9 distinct presets:
    - Viral Glow Pop (Shorts Edit) — Default: Single-layer neon glow, dynamic word colors, snappy pop
    - Hormozi Pop (Classic)
    - MrBeast Viral (Green)
    - Cyberpunk Neon (Gaming)
    - Fire Hype (Red/Orange)
    - Podcast Box (Clean)
    - Gold Luxury (Aesthetic)
    - Minimalist Clean
    - Karaoke Sweep

    Handles silent / no-speech clips gracefully:
    - If *words* is empty, returns a valid minimal ASS script containing a dummy
      invisible dialogue line so libass never encounters an empty file.
    """
    target_w = int(getattr(config, "output_width", _DEFAULT_WIDTH))
    target_h = int(getattr(config, "output_height", _DEFAULT_HEIGHT))
    cfg_margin_v = int(getattr(config, "margin_v", 0))

    # Proportional lower-third / slightly below center sweet spot margin (around 65% down from top)
    if cfg_margin_v <= 0 or cfg_margin_v in (380, 420):
        if target_h >= 1800:
            margin_v = 640  # 9:16 Shorts/Reels (1080x1920) -> ~66.7% down
        elif abs(target_w / max(1, target_h) - 0.8) < 0.05:
            margin_v = 430  # 4:5 Instagram Portrait (1080x1350) -> ~68% down
        elif target_w == target_h:
            margin_v = 340  # 1:1 Square (1080x1080) -> ~68% down
        else:
            margin_v = 240  # 16:9 Landscape (1920x1080) -> ~77% down
    else:
        margin_v = cfg_margin_v

    theme_raw = getattr(config, "theme", "Viral Glow Pop (Shorts Edit)")
    theme = _resolve_theme(theme_raw)

    font_scale = float(getattr(config, "font_scale", 1.0))
    if font_scale <= 0.1:
        font_scale = 1.0

    # If no words are present, return a valid minimal ASS script with a dummy invisible line
    if not words:
        header = build_ass_header(
            config=config,
            target_w=target_w,
            target_h=target_h,
            margin_v=margin_v,
        )
        dummy_event = "Dialogue: 0,0:00:00.00,0:00:00.05,Default,,0,0,0,,{\\alpha&HFF&}\n"
        return header + dummy_event

    # Detect Malayalam / Indic characters
    all_text = " ".join(getattr(w, "text", "") for w in words)
    is_indic = contains_indic(all_text)

    # Base font resolution
    if is_indic:
        font_name = "Nirmala UI"
    else:
        font_name = getattr(config, "font_name", "Arial Black")

    # Configure Preset Parameters
    is_sweep_mode = (theme == "Karaoke Sweep")
    is_viral_glow = (theme == "Viral Glow Pop (Shorts Edit)")

    if is_viral_glow:
        words_per_chunk = 3
        base_font_size = 78
        if not is_indic:
            font_name = "Montserrat Black, Arial Black"
        primary_color = "&H00FFFFFF&"
        secondary_color = "&H0014FF39&"
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 4.0
        shadow = 2.0
        active_prefix = ""
        active_suffix = ""
        inactive_prefix = ""
        inactive_suffix = ""

    elif theme == "MrBeast Viral (Green)":
        words_per_chunk = 3
        base_font_size = 80
        primary_color = "&H00FFFFFF&"
        secondary_color = "&H0039FF14&"  # Bright Neon Lime Green
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 6.0
        shadow = 3.0
        active_prefix = r"{\c&H0039FF14&\3c&H00000000&\fscx122\fscy122\t(0,80,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00FFFFFF&\3c&H00000000&}"
        inactive_suffix = r"{\r}"

    elif theme == "Cyberpunk Neon (Gaming)":
        words_per_chunk = 3
        base_font_size = 74
        primary_color = "&H00FFFF00&"    # Electric Cyan
        secondary_color = "&H00FFFFFF&"  # Pure White
        outline_color = "&H00FF00AA&"    # Neon Magenta outline
        back_color = "&H00FF00AA&"
        border_style = 1
        outline = 4.0
        shadow = 2.0
        active_prefix = r"{\c&H00FFFFFF&\3c&H00FF00FF&\blur4\fscx115\fscy115\t(0,80,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00FFFF00&\3c&H00FF00AA&\blur2}"
        inactive_suffix = r"{\r}"

    elif theme == "Fire Hype (Red/Orange)":
        words_per_chunk = 3
        base_font_size = 78
        primary_color = "&H00FFFFFF&"
        secondary_color = "&H000045FF&"  # Vibrant Fire Orange-Red
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 5.0
        shadow = 3.0
        active_prefix = r"{\c&H000045FF&\3c&H00000000&\fscx120\fscy120\t(0,90,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00FFFFFF&\3c&H00000000&}"
        inactive_suffix = r"{\r}"

    elif theme == "Podcast Box (Clean)":
        words_per_chunk = 4
        base_font_size = 68
        if not is_indic:
            font_name = "Arial"
        primary_color = "&H00FFFFFF&"
        secondary_color = "&H0000C8FF&"  # Warm Amber/Yellow
        outline_color = "&H00000000&"
        back_color = "&H99000000&"        # Semi-transparent dark background box
        border_style = 3                  # Background box mode
        outline = 2.0
        shadow = 0.0
        active_prefix = r"{\c&H0000C8FF&\fscx108\fscy108\t(0,70,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00FFFFFF&}"
        inactive_suffix = r"{\r}"

    elif theme == "Gold Luxury (Aesthetic)":
        words_per_chunk = 4
        base_font_size = 72
        primary_color = "&H0037AFD4&"    # Metallic Gold
        secondary_color = "&H00FFFFFF&"  # Pure White
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 2.0
        shadow = 4.0
        active_prefix = r"{\c&H00FFFFFF&\3c&H00000000&\blur1\fscx110\fscy110\t(0,70,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H0037AFD4&\3c&H00000000&}"
        inactive_suffix = r"{\r}"

    elif theme == "Minimalist Clean":
        words_per_chunk = 4
        base_font_size = 66
        if not is_indic:
            font_name = "Segoe UI"
        primary_color = "&H00F5F5F5&"    # Clean Off-White
        secondary_color = "&H00FFFFFF&"
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 1.5
        shadow = 2.0
        active_prefix = r"{\c&H00FFFFFF&\blur1\fscx106\fscy106\t(0,60,\fscx100\fscy100)}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00F5F5F5&}"
        inactive_suffix = r"{\r}"

    elif theme == "Karaoke Sweep":
        words_per_chunk = 4
        base_font_size = 74
        primary_color = "&H00FFFFFF&"    # Base text color
        secondary_color = "&H0000D7FF&"  # Sweep fill color (Bright Yellow/Gold)
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 4.0
        shadow = 2.0
        active_prefix = ""
        active_suffix = ""
        inactive_prefix = ""
        inactive_suffix = ""

    else:
        # Default: Hormozi Pop (Classic)
        words_per_chunk = 3
        base_font_size = 78
        primary_color = "&H00FFFFFF&"
        user_hl = getattr(config, "highlight_color", "&H0000D7FF")
        secondary_color = hex_to_ass_color(user_hl, "00")
        outline_color = "&H00000000&"
        back_color = "&H00000000&"
        border_style = 1
        outline = 5.0
        shadow = 3.0
        active_prefix = rf"{{\c{secondary_color}\3c&H00000000&\fscx115\fscy115\t(0,80,\fscx100\fscy100)}}"
        active_suffix = r"{\r}"
        inactive_prefix = r"{\c&H00FFFFFF&\3c&H00000000&}"
        inactive_suffix = r"{\r}"

    final_font_size = max(24, int(base_font_size * font_scale))

    header = build_ass_header(
        config=config,
        font_name=font_name,
        font_size=final_font_size,
        primary_color=primary_color,
        secondary_color=secondary_color,
        outline_color=outline_color,
        back_color=back_color,
        border_style=border_style,
        outline=outline,
        shadow=shadow,
        margin_v=margin_v,
        target_w=target_w,
        target_h=target_h,
    )

    # 1. Chunk words into dynamic groups
    chunks: list[list[Word]] = []
    current_chunk: list[Word] = []
    max_gap = float(getattr(config, "max_gap_seconds", 0.45))

    for w in words:
        if current_chunk:
            prev_w = current_chunk[-1]
            gap = w.start - prev_w.end
            if len(current_chunk) >= words_per_chunk or gap > max_gap:
                chunks.append(current_chunk)
                current_chunk = []
        current_chunk.append(w)
    if current_chunk:
        chunks.append(current_chunk)

    events: list[str] = []

    # 2. Render Cues
    if is_sweep_mode:
        # Karaoke Sweep: One line per chunk with progressive \\kf tags
        for chunk in chunks:
            chunk_start = max(0.0, chunk[0].start - start_offset)
            chunk_end = max(chunk_start + 0.2, chunk[-1].end - start_offset)

            sweep_parts: list[str] = []
            for idx, w in enumerate(chunk):
                w_text = format_word_display(w.text, is_indic)
                if not w_text:
                    continue
                # Centiseconds duration
                w_dur_cs = max(1, int(round((w.end - w.start) * 100)))

                # Check gap between previous word and current word
                if idx > 0:
                    prev_end = chunk[idx - 1].end
                    gap_cs = int(round((w.start - prev_end) * 100))
                    if gap_cs > 2:
                        sweep_parts.append(rf"{{\k{gap_cs}}}")

                sweep_parts.append(rf"{{\kf{w_dur_cs}}}{w_text} ")

            line_text = "".join(sweep_parts).strip()
            event = (
                f"Dialogue: 0,"
                f"{format_ass_time(chunk_start)},"
                f"{format_ass_time(chunk_end)},"
                f"Default,,0,0,0,,"
                f"{line_text}"
            )
            events.append(event)

    elif is_viral_glow:
        # Single clean ASS dialogue layer with active word glow pop (Shorts Edit)
        pop_anim = r"{\fscx118\fscy118\t(0,70,\fscx100\fscy100)}"
        global_word_counter = 0

        for chunk in chunks:
            n_words = len(chunk)
            for i, active_w in enumerate(chunk):
                sub_start = max(0.0, active_w.start - start_offset)
                if i + 1 < n_words:
                    sub_end = max(sub_start + 0.05, chunk[i + 1].start - start_offset)
                else:
                    sub_end = max(sub_start + 0.1, active_w.end - start_offset)

                if i == 0:
                    chunk_first_start = max(0.0, chunk[0].start - start_offset)
                    if sub_start > chunk_first_start:
                        sub_start = chunk_first_start

                # Get dynamic vibrant highlight color for the active word
                active_color = _get_viral_word_color(active_w.text, global_word_counter + i)
                start_str = format_ass_time(sub_start)
                end_str = format_ass_time(sub_end)

                parts: list[str] = []
                for j, w in enumerate(chunk):
                    w_text = format_word_display(w.text, is_indic)
                    if not w_text:
                        continue
                    if j == i:
                        parts.append(rf"{{\c{active_color}&\3c&H00000000&\blur3}}{w_text}")
                    else:
                        parts.append(rf"{{\c{_PURE_WHITE}&\3c&H00000000&\blur0}}{w_text}")

                if not parts:
                    continue

                line_text = " ".join(parts)
                event = (
                    f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,"
                    rf"{pop_anim}{line_text}"
                )
                events.append(event)

            global_word_counter += n_words

    else:
        # Dynamic pop animation mode
        for chunk in chunks:
            n_words = len(chunk)
            for i, active_w in enumerate(chunk):
                sub_start = max(0.0, active_w.start - start_offset)
                if i + 1 < n_words:
                    sub_end = max(sub_start + 0.05, chunk[i + 1].start - start_offset)
                else:
                    sub_end = max(sub_start + 0.1, active_w.end - start_offset)

                if i == 0:
                    chunk_first_start = max(0.0, chunk[0].start - start_offset)
                    if sub_start > chunk_first_start:
                        sub_start = chunk_first_start

                parts: list[str] = []
                for j, w in enumerate(chunk):
                    w_text = format_word_display(w.text, is_indic)
                    if not w_text:
                        continue
                    if j == i:
                        parts.append(f"{active_prefix}{w_text}{active_suffix}")
                    else:
                        parts.append(f"{inactive_prefix}{w_text}{inactive_suffix}")

                line_text = " ".join(parts)
                event = (
                    f"Dialogue: 0,"
                    f"{format_ass_time(sub_start)},"
                    f"{format_ass_time(sub_end)},"
                    f"Default,,0,0,0,,"
                    f"{line_text}"
                )
                events.append(event)

    if not events:
        events.append("Dialogue: 0,0:00:00.00,0:00:00.05,Default,,0,0,0,,{\\alpha&HFF&}")

    return header + "\n".join(events) + "\n"


class CaptionGenerator:
    """Object-oriented interface for generating ASS captions."""

    def __init__(self, config: EditorConfig) -> None:
        self.config = config

    def generate(self, words: Sequence[Word], start_offset: float = 0) -> str:
        return generate_ass_subtitles(words, self.config, start_offset=start_offset)
