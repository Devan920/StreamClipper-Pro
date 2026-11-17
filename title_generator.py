"""Automatic English Viral YouTube Title and Hashtag Generator.

Generates catchy, high-CTR English YouTube Shorts / Video titles (under 70 characters)
and 4-5 relevant trending English hashtags for every rendered clip.

Features:
- Optional Gemini AI generation via `google.genai` SDK or REST API:
  Translates and summarizes clips from any language (Malayalam, Hindi, Spanish, etc.)
  into punchy, viral English shorts titles.
- Smart Offline Fallback:
  Uses transcript NLP heuristics, hook templates, keyword extraction, and hype intensity
  to generate engaging titles completely offline without requiring an API key.
- File Output:
  Saves metadata to a companion ``.txt`` file alongside the exported ``.mp4`` clip.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import DEFAULT_GEMINI_API_KEY

logger = logging.getLogger(__name__)

# Fallback viral English hooks by category
_HIGH_HYPE_HOOKS = [
    "You Won't Believe What Just Happened! 🔥",
    "Nobody Saw This Coming! 💀",
    "Wait For The Ending... 🤯",
    "This Clip Went Completely Viral! 🚀",
    "The Most Insane Moment on Stream! 😱",
    "When Everything Went Wrong! 😂",
    "The Most Clutch Play Ever?! ⚡",
    "Pure Chaos on Live Stream! 💥",
]

_MOMENT_HOOKS = [
    "Wait... Did That Really Just Happen?! 😳",
    "Watch Until The End! 🎯",
    "The Greatest Move of All Time! 🏆",
    "Streamer's Reaction Was Priceless! 🤣",
    "How Did They Pull This Off?! 🤯",
    "This Is Peak Entertainment! ✨",
    "Instant Regret on Live Stream! 💀",
]

_DEFAULT_HASHTAGS = ["#Shorts", "#Viral", "#Trending", "#Highlights", "#LiveStream"]

_TOPIC_HASHTAGS = {
    "gaming": ["#Shorts", "#Gaming", "#Viral", "#Gamer", "#Clutch"],
    "funny": ["#Shorts", "#Funny", "#Viral", "#Comedy", "#Lol"],
    "reaction": ["#Shorts", "#Reaction", "#Viral", "#Trending", "#Streamer"],
    "podcast": ["#Shorts", "#Podcast", "#Viral", "#LifeAdvice", "#Inspire"],
    "music": ["#Shorts", "#Music", "#Live", "#Viral", "#Vibes"],
}


def _clean_title(title: str, max_length: int = 70) -> str:
    """Sanitize and clamp title length under max_length at a word boundary."""
    t = title.strip().replace("\n", " ").replace("\r", "")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r'^(Title\s*:\s*|English Title\s*:\s*|["\'])', "", t, flags=re.IGNORECASE)
    t = t.rstrip('"\'')
    if len(t) <= max_length:
        return t

    # Truncate at word boundary
    truncated = t[: max_length - 3]
    last_space = truncated.rfind(" ")
    if last_space > 20:
        truncated = truncated[:last_space]
    return truncated.strip() + "..."


def _extract_keywords(text: str, max_words: int = 4) -> List[str]:
    """Extract prominent English keywords from text, ignoring stopwords."""
    stopwords = {
        "the", "and", "is", "in", "to", "of", "a", "an", "this", "that",
        "it", "for", "on", "with", "as", "at", "by", "from", "up", "about",
        "into", "over", "after", "you", "they", "we", "he", "she", "i",
        "my", "me", "your", "what", "how", "why", "when", "where", "who",
        "so", "no", "yes", "not", "just", "like", "get", "got", "can",
        "have", "has", "had", "will", "would", "do", "did", "are", "was",
        "were", "be", "been", "being", "here", "there", "then", "now",
    }
    words = re.findall(r"\b[A-Za-z]{3,}\b", text.lower())
    freq: Dict[str, int] = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.keys(), key=lambda k: freq[k], reverse=True)
    return [w.capitalize() for w in sorted_words[:max_words]]


def generate_offline_title(
    transcript_text: str = "",
    stream_title: str = "",
    hype_score: float = 0.0,
) -> Dict[str, str]:
    """Generate an engaging English title + hashtags without an internet connection.

    Uses keyword extraction, sentiment/intensity heuristics, and proven viral hook templates.
    """
    combined = f"{stream_title} {transcript_text}".strip()
    keywords = _extract_keywords(combined, max_words=3)

    # Determine topic / style
    lower_comb = combined.lower()
    chosen_hashtags = list(_DEFAULT_HASHTAGS)
    for topic, tags in _TOPIC_HASHTAGS.items():
        if topic in lower_comb:
            chosen_hashtags = list(tags)
            break

    # Select best hook based on hype score and text
    import random
    rng = random.Random(abs(hash(combined)) % 10000)

    if hype_score >= 70 or any(w in lower_comb for w in ["insane", "crazy", "omg", "unbelievable", "wtf", "no way"]):
        base_hook = rng.choice(_HIGH_HYPE_HOOKS)
    else:
        base_hook = rng.choice(_MOMENT_HOOKS)

    # If stream title has a recognizable name or game topic, blend it in
    clean_stream = re.sub(r"[\[\(].*?[\]\)]", "", stream_title).strip()
    stream_kw = _extract_keywords(clean_stream, max_words=2)

    if stream_kw and len(stream_kw) >= 1:
        topic_str = " ".join(stream_kw)
        candidate = f"{base_hook} | {topic_str}"
        if len(candidate) <= 70:
            title = candidate
        else:
            title = base_hook
    elif keywords:
        kw_str = " ".join(keywords[:2])
        candidate = f"When {kw_str} Happened! 🔥"
        if len(candidate) <= 70 and len(candidate) > 15:
            title = candidate
        else:
            title = base_hook
    else:
        title = base_hook

    title = _clean_title(title, max_length=70)
    hashtags_str = " ".join(chosen_hashtags)

    return {
        "title": title,
        "hashtags": hashtags_str,
        "full_title": f"{title} {hashtags_str}",
        "source": "offline",
    }


def generate_gemini_title(
    api_key: str,
    transcript_text: str = "",
    stream_title: str = "",
    hype_score: float = 0.0,
    model_name: str = "gemini-2.5-flash",
) -> Optional[Dict[str, str]]:
    """Call the standard Gemini generateContent REST endpoint to produce viral English title & hashtags.

    Even if the transcript is in Malayalam, Hindi, Spanish, etc., the model
    translates and summarizes the hook into viral English.
    """
    if not api_key or not api_key.strip():
        return None

    api_key = api_key.strip()
    if not api_key.startswith("AIza"):
        logger.info("Gemini API unavailable/invalid key — using offline title generator.")
        return None

    prompt = (
        "You are an elite YouTube Shorts and viral video strategist.\n"
        "Generate a single ultra-engaging, high-CTR English title (strictly UNDER 70 characters) "
        "and 4 to 5 relevant trending English hashtags for a short viral video clip.\n\n"
        f"Stream / Video Title: {stream_title or 'Live Stream'}\n"
        f"Hype Score Intensity: {hype_score:.1f} / 100\n"
        f"Clip Spoken Transcript: {transcript_text or '(Intense gameplay / stream moment without words)'}\n\n"
        "CRITICAL INSTRUCTIONS:\n"
        "1. Always output ONLY English, even if the transcript audio is in Malayalam, Hindi, Japanese, Spanish, etc.\n"
        "2. The title MUST be under 70 characters and include 1 or 2 relevant emojis.\n"
        "3. Provide exactly 4-5 trending English hashtags (starting with #Shorts).\n"
        "4. Output format MUST be strictly:\n"
        "TITLE: <Your English Title Here>\n"
        "HASHTAGS: #Shorts #<Tag2> #<Tag3> #<Tag4> #<Tag5>\n"
    )

    models_to_try = [model_name, "gemini-2.5-flash", "gemini-2.0-flash"]
    seen = set()
    unique_models = []
    for m in models_to_try:
        if m and m not in seen:
            seen.add(m)
            unique_models.append(m)

    for m in unique_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200},
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code in (400, 403, 404):
                logger.info(
                    "Gemini API unavailable/invalid key (HTTP %d) — using offline title generator.",
                    resp.status_code,
                )
                return None

            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    text = parts[0].get("text", "")
                    parsed = _parse_model_output(text)
                    if parsed:
                        parsed["source"] = f"gemini ({m})"
                        return parsed
        except requests.exceptions.RequestException as req_err:
            logger.info("Gemini API unavailable/request error (%s) — using offline title generator: %s", m, req_err)
            return None
        except Exception as exc:
            logger.debug("Gemini REST model %s failed: %s", m, exc)
            continue

    return None


def _parse_model_output(text: str) -> Optional[Dict[str, str]]:
    """Parse TITLE: and HASHTAGS: from Gemini response text."""
    if not text:
        return None

    title_match = re.search(r"TITLE\s*:\s*(.+)", text, re.IGNORECASE)
    tags_match = re.search(r"HASHTAGS\s*:\s*(.+)", text, re.IGNORECASE)

    if title_match:
        raw_title = title_match.group(1).strip()
    else:
        # Fallback to first non-empty line
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        raw_title = lines[0] if lines else ""

    clean_title = _clean_title(raw_title, max_length=70)

    if tags_match:
        raw_tags = tags_match.group(1).strip()
        tags = [t.strip() for t in raw_tags.split() if t.startswith("#")]
        if not tags:
            tags = _DEFAULT_HASHTAGS
        hashtags = " ".join(tags[:5])
    else:
        hashtags = " ".join(_DEFAULT_HASHTAGS)

    if not hashtags.lower().startswith("#shorts"):
        hashtags = f"#Shorts {hashtags}"

    return {
        "title": clean_title,
        "hashtags": hashtags,
        "full_title": f"{clean_title} {hashtags}",
        "source": "gemini",
    }


def generate_clip_title(
    transcript_text: str = "",
    stream_title: str = "",
    gemini_api_key: Optional[str] = None,
    hype_score: float = 0.0,
    model_name: str = "gemini-2.5-flash",
) -> Dict[str, str]:
    """Master helper to generate English viral title & hashtags with offline fallback.

    Parameters
    ----------
    transcript_text:
        Spoken words or text from the clip transcript.
    stream_title:
        Title of the YouTube live stream or VOD.
    gemini_api_key:
        Optional Gemini API key for AI generation.
    hype_score:
        Hype intensity (0 - 100).
    model_name:
        Model name to use when API key is provided.

    Returns
    -------
    dict
        Contains ``title``, ``hashtags``, ``full_title``, and ``source``.
    """
    effective_key = (gemini_api_key or "").strip() or DEFAULT_GEMINI_API_KEY
    if effective_key:
        try:
            ai_result = generate_gemini_title(
                api_key=effective_key,
                transcript_text=transcript_text,
                stream_title=stream_title,
                hype_score=hype_score,
                model_name=model_name,
            )
            if ai_result:
                logger.info("Generated title via %s: %s", ai_result.get("source"), ai_result.get("title"))
                return ai_result
        except Exception as exc:
            logger.warning("Gemini AI title generation error: %s; falling back to offline generator.", exc)

    offline_result = generate_offline_title(
        transcript_text=transcript_text,
        stream_title=stream_title,
        hype_score=hype_score,
    )
    logger.info("Generated title via offline heuristic: %s", offline_result.get("title"))
    return offline_result


def save_clip_metadata(output_video_path: Path, title_info: Dict[str, str]) -> Path:
    """Save title and hashtags to a .txt file alongside the rendered .mp4 clip."""
    output_video_path = Path(output_video_path)
    txt_path = output_video_path.with_suffix(".txt")
    content = (
        f"Title:\n{title_info.get('title', '')}\n\n"
        f"Hashtags:\n{title_info.get('hashtags', '')}\n\n"
        f"Full:\n{title_info.get('full_title', '')}\n\n"
        f"Source: {title_info.get('source', 'offline')}\n"
    )
    try:
        txt_path.write_text(content, encoding="utf-8")
        logger.debug("Saved clip title metadata to %s", txt_path)
    except Exception as exc:
        logger.warning("Could not write clip title metadata to %s: %s", txt_path, exc)
    return txt_path
