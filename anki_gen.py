#!/usr/bin/env python3
"""
anki_gen.py — Generate Anki flashcards from a URL using Claude AI.

Pulls figures and images from the source as first-class inputs alongside text.
Claude studies the visuals first to identify the core concepts, then uses the
text to fill in supporting facts. Cards can reference source images, which get
uploaded into Anki's media library and embedded into the card.

Usage:
    python anki_gen.py <url>
    python anki_gen.py <url> --deck "My Deck"

Supports: web articles, blog posts, PDFs (academic papers), YouTube videos
(transcript only — frame extraction is not yet supported).
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import anthropic
import requests
from bs4 import BeautifulSoup

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    YOUTUBE_AVAILABLE = True
except ImportError:
    YOUTUBE_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

ANKI_CONNECT_URL = "http://localhost:8765"
GUIDELINES_FILE = Path(__file__).parent / "flashcard_guidelines.md"

# Available Claude models. Keep in cost order (most expensive first).
# Each model supports vision so we can use them interchangeably.
AVAILABLE_MODELS = {
    "claude-opus-4-5":   {"label": "Opus 4.5",   "tier": "highest quality"},
    "claude-sonnet-4-5": {"label": "Sonnet 4.5", "tier": "balanced"},
    "claude-haiku-4-5":  {"label": "Haiku 4.5",  "tier": "fastest & cheapest"},
}
DEFAULT_MODEL = "claude-opus-4-5"


def resolve_model(name):
    """Return a known model name, falling back to DEFAULT_MODEL if invalid."""
    if isinstance(name, str) and name in AVAILABLE_MODELS:
        return name
    return DEFAULT_MODEL

MAX_IMAGES_PER_DOC = 12
MIN_IMAGE_DIM = 200       # skip anything smaller than this on either axis
MAX_IMAGE_LONG_EDGE = 1568  # Anthropic vision recommendation
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB hard cap per image after resize

# URL-path substrings that almost always indicate a non-figure image
IMAGE_BLOCKLIST_SUBSTRINGS = (
    "icon", "logo", "avatar", "sprite", "tracker", "pixel", "analytics",
    "advertisement", "/ad/", "/ads/", "share-", "social", "favicon",
)


# ── AnkiConnect helpers ────────────────────────────────────────────────────────

def anki_request(action, **params):
    payload = {"action": action, "version": 6, "params": params}
    resp = requests.post(ANKI_CONNECT_URL, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


def ensure_deck(deck_name):
    decks = anki_request("deckNames")
    if deck_name not in decks:
        anki_request("createDeck", deck=deck_name)
        print(f"  Created deck: {deck_name}")


def _upload_images_to_anki(images):
    """Store each image in Anki's media library; return {idx: filename}."""
    filename_map = {}
    for idx, img in enumerate(images):
        data = img.get("data")
        if not data or not isinstance(data, (bytes, bytearray)):
            continue
        ext = img.get("ext") or _ext_from_mime(img.get("mime", "image/png"))
        digest = hashlib.sha1(data).hexdigest()[:16]
        filename = f"_anki-gen-{digest}.{ext}"
        b64 = base64.b64encode(data).decode("ascii")
        actual = anki_request("storeMediaFile", filename=filename, data=b64)
        filename_map[idx] = actual or filename
    return filename_map


def _ext_from_mime(mime):
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(mime.lower(), "png")


def _s(value):
    """Coerce a field value to string. Anki rejects None / non-string fields
    with 'bad argument type for built-in operation'."""
    return "" if value is None else str(value)


def normalize_deck_name(value, default="AI Generated"):
    """Coerce a deck name (which arrives via JSON / multipart) to a clean
    non-empty string. Defensive against non-string types from the client."""
    s = "" if value is None else str(value)
    s = s.strip()
    return s or default


def card_to_note(card, deck_name, image_filename_map=None):
    """Convert a card dict into an AnkiConnect note payload, embedding any
    referenced image into the appropriate field.

    image_filename_map: {image_idx: stored_anki_filename}
    """
    image_filename_map = image_filename_map or {}
    image_ref = card.get("image_ref")
    image_html = ""
    if image_ref is not None and image_ref in image_filename_map:
        fn = image_filename_map[image_ref]
        image_html = f'<div><img src="{fn}"></div>'

    ctype = card.get("type", "basic").lower()
    if ctype == "cloze":
        # Image goes inside the Text field (visible on both sides of a cloze card)
        text = image_html + _s(card.get("text"))
        return {
            "deckName": deck_name,
            "modelName": "Cloze",
            "fields": {
                "Text": text,
                "Back Extra": _s(card.get("extra")),
            },
            "options": {"allowDuplicate": False},
            "tags": ["anki-gen"],
        }
    # default → basic: image goes on Front (visual as cue)
    return {
        "deckName": deck_name,
        "modelName": "Basic",
        "fields": {
            "Front": image_html + _s(card.get("question")),
            "Back": _s(card.get("answer")),
        },
        "options": {"allowDuplicate": False},
        "tags": ["anki-gen"],
    }


def add_cards_to_anki(cards, deck_name, images=None):
    """Upload referenced images, then add cards to Anki."""
    images = images or []
    used_refs = sorted({
        c.get("image_ref") for c in cards
        if c.get("image_ref") is not None and 0 <= c.get("image_ref") < len(images)
    })
    # storeMediaFile only the images that are actually used
    image_filename_map = {}
    if used_refs:
        used_imgs = [images[i] for i in used_refs]
        partial_map = _upload_images_to_anki(used_imgs)
        # partial_map keys are positions in used_refs (0..n-1); remap to original refs
        for local_idx, orig_idx in enumerate(used_refs):
            if local_idx in partial_map:
                image_filename_map[orig_idx] = partial_map[local_idx]

    notes = [card_to_note(c, deck_name, image_filename_map) for c in cards]
    result = anki_request("addNotes", notes=notes)
    added = sum(1 for r in result if r is not None)
    skipped = len(result) - added
    return added, skipped


# ── URL routing ────────────────────────────────────────────────────────────────

def extract_youtube_id(url):
    parsed = urlparse(url)
    if "youtu.be" in parsed.netloc:
        return parsed.path.lstrip("/").split("?")[0]
    if "youtube.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


def is_pdf_url(url):
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return True
    # arxiv: /pdf/<id> (no extension) is also a PDF
    if "arxiv.org" in urlparse(url).netloc and path.startswith("/pdf/"):
        return True
    return False


def canonicalize_url(url):
    """Rewrite URLs that point to an abstract/landing page → the actual PDF."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path

    # arxiv.org/abs/<id>  →  arxiv.org/pdf/<id>.pdf
    if "arxiv.org" in netloc and path.startswith("/abs/"):
        paper_id = path[len("/abs/"):].rstrip("/")
        return f"https://arxiv.org/pdf/{paper_id}.pdf"

    # arxiv.org/pdf/<id> (no extension)  →  arxiv.org/pdf/<id>.pdf
    if "arxiv.org" in netloc and path.startswith("/pdf/") and not path.endswith(".pdf"):
        return f"https://arxiv.org{path.rstrip('/')}.pdf"

    return url


# ── Image processing ───────────────────────────────────────────────────────────

def _looks_like_decoration(url):
    """Heuristic: is this URL probably an icon/logo/tracker, not a real figure?"""
    low = url.lower()
    return any(s in low for s in IMAGE_BLOCKLIST_SUBSTRINGS)


def _normalize_image(data, mime_hint=None, min_dim=MIN_IMAGE_DIM):
    """Resize-if-needed and re-encode as JPEG/PNG.

    Returns (data: bytes, mime: str, ext: str) or None if image is invalid /
    too small / can't be processed.

    min_dim: pass `None` to skip the small-image filter (use for user-pasted
    images where intent is explicit).
    """
    if not PIL_AVAILABLE:
        # Pass through without filtering — not ideal but works.
        if len(data) > MAX_IMAGE_BYTES:
            return None
        ext = _ext_from_mime(mime_hint or "image/png")
        return (data, mime_hint or "image/png", ext)

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None

    w, h = img.size
    if min_dim is not None and (w < min_dim or h < min_dim):
        return None

    # Convert palette/transparency to RGB for clean JPEG encoding
    if img.mode in ("P", "LA", "RGBA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "RGBA" or img.mode == "LA":
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Resize to fit MAX_IMAGE_LONG_EDGE
    long_edge = max(w, h)
    if long_edge > MAX_IMAGE_LONG_EDGE:
        scale = MAX_IMAGE_LONG_EDGE / long_edge
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    out = buf.getvalue()
    if len(out) > MAX_IMAGE_BYTES:
        return None
    return (out, "image/jpeg", "jpg")


def _download_and_process(image_url, caption="", source_kind="web"):
    """Download an image URL and normalize it. Returns image dict or None."""
    if _looks_like_decoration(image_url):
        return None
    try:
        r = requests.get(image_url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": image_url,
        })
        r.raise_for_status()
    except Exception:
        return None
    content_type = r.headers.get("content-type", "").lower().split(";")[0].strip()
    if not content_type.startswith("image/"):
        return None
    normalized = _normalize_image(r.content, content_type)
    if not normalized:
        return None
    data, mime, ext = normalized
    return {
        "data": data,
        "mime": mime,
        "ext": ext,
        "caption": caption.strip()[:300],
        "source_url": image_url,
        "source_kind": source_kind,
    }


# ── Content fetching ───────────────────────────────────────────────────────────

EMPTY_CONTENT = {"text": "", "images": [], "title": ""}


def _clean_title(title, max_len=200):
    """Normalize whitespace in a title; preserve the wording verbatim.

    We only collapse whitespace (multi-space / newlines → single space) and
    apply a generous length cap as a sanity bound. NO suffix stripping —
    the source's title is used as-is so the deck name matches the original
    material exactly.
    """
    if not title:
        return ""
    return " ".join(title.split())[:max_len].strip()


def _title_from_url(url):
    """Fallback: derive a title from the URL's path."""
    try:
        parsed = urlparse(url)
        last = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc
        # Strip extensions like .pdf, .html
        last = re.sub(r"\.[a-z0-9]{2,5}$", "", last, flags=re.IGNORECASE)
        return last.replace("-", " ").replace("_", " ").strip()[:80]
    except Exception:
        return ""


def _fetch_youtube_transcript_segments(video_id):
    """Fetch raw transcript segments with timestamps."""
    if not YOUTUBE_AVAILABLE:
        raise RuntimeError(
            "youtube-transcript-api is not installed. "
            "Run: pip install youtube-transcript-api"
        )
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)
    except Exception as e:
        raise RuntimeError(
            f"Could not fetch transcript for video {video_id}. "
            f"The video may have captions disabled or be unavailable. "
            f"({type(e).__name__}: {e})"
        ) from e
    segments = []
    for s in fetched:
        text = getattr(s, "text", None) or (s.get("text", "") if isinstance(s, dict) else "")
        start = getattr(s, "start", None) or (s.get("start", 0.0) if isinstance(s, dict) else 0.0)
        if text:
            segments.append({"start": float(start), "text": text})
    return segments


def _format_transcript_with_timestamps(segments):
    """Render transcript segments as '[mm:ss] text' lines for prompts."""
    lines = []
    for s in segments:
        m, sec = divmod(int(s["start"]), 60)
        h, m = divmod(m, 60)
        ts = f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"
        lines.append(f"[{ts}] {s['text']}")
    return "\n".join(lines)


def fetch_youtube_transcript(video_id):
    """Backwards-compat: transcript only, no frames (used as a fallback)."""
    segments = _fetch_youtube_transcript_segments(video_id)
    return {"text": " ".join(s["text"] for s in segments), "images": []}


# ── YouTube frame extraction ──────────────────────────────────────────────────

PICK_MOMENTS_PROMPT = """\
You are scanning a YouTube video transcript to identify moments where a
VISUAL element (slide, diagram, demo, graph, code, animation) is likely
being shown — places where a screenshot would be informative.

Transcript (timestamps in mm:ss or h:mm:ss):
{transcript}

Look for cues like:
- "as you can see here", "look at this", "this shows", "in this graph"
- "let me show you", "on the screen", "this slide"
- references to specific objects: "the equation here", "this code", "the diagram"
- transitions like "next, ...", "now consider..." that often map to new slides

Return up to {max_moments} of the strongest candidates. Skip introductions,
outros, banter, and sections that are purely verbal explanation with no
visual reference.

Return ONLY a JSON array — no markdown, no commentary:
[{{"t": 73.5, "reason": "shows the diagram of X"}}, ...]

where `t` is the timestamp in SECONDS (a number).
"""


def _pick_visual_moments(segments, max_moments=12):
    """Ask Claude to identify timestamps where a visual is referenced."""
    transcript = _format_transcript_with_timestamps(segments)
    client = anthropic.Anthropic()
    prompt = PICK_MOMENTS_PROMPT.format(
        transcript=transcript[:30000],
        max_moments=max_moments,
    )
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    moments = json.loads(raw)
    # Normalize and clamp
    out = []
    for m in moments:
        try:
            t = float(m.get("t", 0))
        except (TypeError, ValueError):
            continue
        if t < 1:  # skip very-start timestamps (usually intro)
            continue
        out.append({"t": t, "reason": str(m.get("reason", ""))[:200]})
    return out[:max_moments]


def _have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def _download_youtube_video(url, max_height=480):
    """Download a YouTube video at low resolution to a temp file. Returns (path, tmpdir)."""
    if not YTDLP_AVAILABLE:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp")
    tmpdir = tempfile.mkdtemp(prefix="ankigen_yt_")
    out_template = os.path.join(tmpdir, "video.%(ext)s")
    ydl_opts = {
        "format": f"bv*[height<={max_height}][ext=mp4]/bv*[height<={max_height}]/best[height<={max_height}]",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Could not download video: {e}") from e

    files = [f for f in os.listdir(tmpdir) if f.startswith("video.")]
    if not files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("yt-dlp finished but no video file was produced.")
    return os.path.join(tmpdir, files[0]), tmpdir


def _extract_frame(video_path, t_seconds, out_path):
    """Use ffmpeg to extract a single frame at the given timestamp."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{t_seconds:.2f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg failed at t={t_seconds}: {e.stderr.decode()[:200]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError(f"ffmpeg produced no/empty frame at t={t_seconds}")
    return out_path


CURATE_FRAMES_PROMPT = """\
Below are {n} candidate frames extracted from a YouTube video, in order.
For each frame, decide whether it's WORTH KEEPING as a flashcard visual.

KEEP if the frame shows:
- A slide with diagrams, equations, charts, code, or substantive text
- A demo of code, UI, an experiment, or a process
- A whiteboard / illustration / sketched figure
- An animation or visualization conveying a concept
- Any informative visual element

SKIP if the frame shows:
- Just the speaker's face / talking head, with no useful background
- Generic stock footage, b-roll, or scenery
- Title cards, channel intros/outros, sponsor segments, logos
- Empty or near-empty backgrounds
- Blurry frames or mid-transition frames

For each frame, give a tight caption (max 15 words) IF kept.

Context — each frame's transcript moment:
{frame_context}

Return ONLY a JSON array — no markdown, no commentary:
[{{"index": 0, "keep": true, "caption": "..."}}, {{"index": 1, "keep": false, "caption": ""}}, ...]

Include exactly {n} entries, one per frame, in order.
"""


def _curate_frames(frame_dicts, moments):
    """Ask Claude to keep/skip each extracted frame. frame_dicts: [{data, mime, ...}]"""
    if not frame_dicts:
        return []
    client = anthropic.Anthropic()
    context_lines = []
    for i, m in enumerate(moments):
        t = m["t"]
        mm, ss = divmod(int(t), 60)
        h, mm = divmod(mm, 60)
        ts = f"{h}:{mm:02d}:{ss:02d}" if h else f"{mm}:{ss:02d}"
        context_lines.append(f"  [{i}] at {ts} — {m.get('reason', '')}")
    prompt_text = CURATE_FRAMES_PROMPT.format(
        n=len(frame_dicts),
        frame_context="\n".join(context_lines),
    )
    blocks = []
    for img in frame_dicts:
        data = img.get("data")
        if not isinstance(data, (bytes, bytearray)):
            continue  # defensive: only base64-encode actual bytes
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("mime", "image/jpeg"),
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
    blocks.append({"type": "text", "text": prompt_text})

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": blocks}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    decisions = json.loads(raw)

    kept = []
    for d in decisions:
        try:
            idx = int(d["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if not d.get("keep"):
            continue
        if not (0 <= idx < len(frame_dicts)):
            continue
        frame = dict(frame_dicts[idx])
        frame["caption"] = (d.get("caption") or "").strip()[:300] or frame.get("caption", "")
        kept.append(frame)
    return kept


def _fetch_youtube_title(url):
    """Best-effort fetch of a YouTube video's title via yt-dlp metadata-only."""
    if not YTDLP_AVAILABLE:
        return ""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return _clean_title((info or {}).get("title", ""))
    except Exception:
        return ""


def fetch_youtube_content(url, video_id, *, progress=None):
    """Full YouTube pipeline: transcript + visual moments + curated frames.

    Keeps the downloaded video file alive (callers must call cleanup_content)
    so individual cards can be enriched with additional frames later.

    Returns: {"text", "images", "warnings", "title", "_video_path",
              "_video_tmpdir", "_segments", "_url"}
    """
    def _p(msg):
        if progress:
            try: progress(msg)
            except Exception: pass
        print(f"      {msg}", flush=True)

    warnings = []
    title = _fetch_youtube_title(url)
    segments = _fetch_youtube_transcript_segments(video_id)
    transcript_text = " ".join(s["text"] for s in segments)
    base = {
        "text": transcript_text, "images": [], "warnings": warnings,
        "title": title,
        "_segments": segments, "_url": url,
    }

    if not _have_ffmpeg():
        warnings.append("⚠ Frame extraction skipped: ffmpeg is not installed. Install with `brew install ffmpeg`.")
        _p(warnings[-1])
        return base
    if not YTDLP_AVAILABLE:
        warnings.append("⚠ Frame extraction skipped: yt-dlp is not installed. Run `pip install yt-dlp`.")
        _p(warnings[-1])
        return base

    _p(f"Transcript: {len(segments)} segments, {len(transcript_text):,} chars.")
    if not segments:
        warnings.append("⚠ No transcript available — frame extraction cannot target specific moments. Try the 'Enrich with visuals' button on individual cards to pull frames anyway.")
        _p(warnings[-1])
        return base

    _p("Identifying key visual moments in the transcript…")
    try:
        moments = _pick_visual_moments(segments)
    except Exception as e:
        warnings.append(f"⚠ Could not identify visual moments ({type(e).__name__}: {e})")
        _p(warnings[-1])
        return base
    _p(f"Found {len(moments)} candidate moment(s).")
    if not moments:
        warnings.append("⚠ No visual moments detected in the transcript (mostly verbal content?). Use 'Enrich with visuals' per-card to force extraction.")
        _p(warnings[-1])
        return base

    _p("Downloading video (low-res)…")
    try:
        video_path, tmpdir = _download_youtube_video(url)
    except Exception as e:
        warnings.append(f"⚠ Video download failed ({type(e).__name__}: {e}). The video may be private, age-restricted, or region-locked.")
        _p(warnings[-1])
        return base

    # From here on, video is cached. Caller cleans it up via cleanup_content().
    base["_video_path"] = video_path
    base["_video_tmpdir"] = tmpdir

    _p(f"Extracting {len(moments)} frame(s) with ffmpeg…")
    raw_frames = []
    for i, m in enumerate(moments):
        frame_path = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
        try:
            _extract_frame(video_path, m["t"], frame_path)
        except Exception:
            continue
        with open(frame_path, "rb") as fh:
            raw = fh.read()
        normalized = _normalize_image(raw, "image/jpeg")
        if not normalized:
            continue
        data, mime, ext = normalized
        raw_frames.append({
            "data": data, "mime": mime, "ext": ext,
            "caption": m.get("reason", ""),
            "source_url": url,
            "source_kind": "youtube",
            "_moment_idx": i,
        })
    _p(f"Successfully extracted {len(raw_frames)} frame(s).")
    if not raw_frames:
        warnings.append("⚠ ffmpeg extracted 0 usable frames. Try 'Enrich with visuals' per-card to retry.")
        _p(warnings[-1])
        return base

    _p("Curating frames (keeping informative ones)…")
    try:
        kept_frames = _curate_frames(raw_frames, moments)
    except Exception as e:
        warnings.append(f"⚠ Frame curation failed ({e}) — keeping all extracted frames.")
        kept_frames = raw_frames
    _p(f"Kept {len(kept_frames)} of {len(raw_frames)} frame(s).")
    if not kept_frames:
        warnings.append("⚠ Curation rejected all frames as uninformative (talking heads, intros, etc.). Use 'Enrich with visuals' per-card to retry with a card-specific search.")

    for frame in kept_frames:
        frame.pop("_moment_idx", None)
    base["images"] = kept_frames
    return base


def cleanup_content(content):
    """Delete any temp resources held by a content dict (e.g., cached video)."""
    if not isinstance(content, dict):
        return
    tmpdir = content.get("_video_tmpdir")
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_web_content(url):
    """Fetch a web page: extract main text AND figures/images.

    Returns: {"text": str, "images": [...], "title": str}.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Extract title BEFORE stripping nav/header (which sometimes hold it) ──
    title = ""
    if soup.title and soup.title.string:
        title = _clean_title(soup.title.string)
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = _clean_title(og_title["content"])
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = _clean_title(h1.get_text(" ", strip=True))
    if not title:
        title = _title_from_url(url)

    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    # ── Images: prioritize <figure>, then any <img> inside main/article ──
    images = []
    seen_urls = set()

    def _add_img_tag(img_tag, caption=""):
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src or src.startswith("data:"):
            return
        absolute = urljoin(url, src)
        if absolute in seen_urls:
            return
        seen_urls.add(absolute)
        alt = img_tag.get("alt", "")
        cap = caption or alt
        img = _download_and_process(absolute, caption=cap, source_kind="web")
        if img:
            images.append(img)

    # Pass 1: <figure> elements (canonical "this is a real figure")
    for fig in soup.find_all("figure"):
        if len(images) >= MAX_IMAGES_PER_DOC:
            break
        caption_tag = fig.find("figcaption")
        caption_text = caption_tag.get_text(" ", strip=True) if caption_tag else ""
        for img_tag in fig.find_all("img"):
            if len(images) >= MAX_IMAGES_PER_DOC:
                break
            _add_img_tag(img_tag, caption=caption_text)

    # Pass 2: <img> tags inside main content
    if len(images) < MAX_IMAGES_PER_DOC:
        main = soup.find("main") or soup.find("article") or soup.body or soup
        for img_tag in main.find_all("img"):
            if len(images) >= MAX_IMAGES_PER_DOC:
                break
            _add_img_tag(img_tag)

    # ── Text ──
    main = soup.find("main") or soup.find("article") or soup.body
    text = (main or soup).get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()

    return {"text": text[:20000], "images": images, "title": title}


def fetch_pdf_content(url):
    """Fetch a PDF and extract its text and embedded images."""
    if not PDF_AVAILABLE:
        raise RuntimeError(
            "PyMuPDF is not installed. Run: pip install pymupdf"
        )
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if not resp.content:
        return dict(EMPTY_CONTENT)

    # Verify the response is actually a PDF (some sites block scrapers and
    # return a CAPTCHA / login page disguised by the URL extension).
    ctype = resp.headers.get("content-type", "").lower()
    if not (resp.content[:5] == b"%PDF-" or "application/pdf" in ctype):
        raise RuntimeError(
            f"The URL did not return a PDF (Content-Type: {ctype or 'unknown'}). "
            "The site is likely blocking scrapers (CAPTCHA, login wall, or "
            "bot detection). Download the PDF manually and use the Upload PDF "
            "option, or try a different mirror (the publisher's site, arxiv, "
            "or institutional repository)."
        )

    return _extract_pdf_bytes(resp.content)


def fetch_pdf_from_bytes(pdf_bytes, source_label="uploaded.pdf"):
    """Parse a PDF from raw bytes (e.g. an uploaded file)."""
    if not PDF_AVAILABLE:
        raise RuntimeError("PyMuPDF is not installed. Run: pip install pymupdf")
    if not pdf_bytes:
        return dict(EMPTY_CONTENT)
    if pdf_bytes[:5] != b"%PDF-":
        raise RuntimeError("Uploaded file does not look like a PDF (missing %PDF- header).")
    out = _extract_pdf_bytes(pdf_bytes)
    # If PDF had no usable metadata title, fall back to the filename
    if not out.get("title"):
        # Strip path + extension
        name = re.sub(r"\.pdf$", "", os.path.basename(source_label or ""), flags=re.IGNORECASE)
        out["title"] = _clean_title(name.replace("_", " ").replace("-", " "))
    return out


def _extract_pdf_bytes(pdf_bytes):
    """Shared PDF text + image extraction. Operates on raw PDF bytes."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text_parts = []
    images = []
    seen_hashes = set()

    # ── Title: PDF metadata first, then first-page heuristic ──
    title = ""
    meta = doc.metadata or {}
    raw_title = (meta.get("title") or "").strip()
    # PDF metadata "title" is often garbage like "untitled1.dvi" or "Microsoft Word - ...";
    # accept it only if it looks reasonable
    if raw_title and len(raw_title) >= 4 and not raw_title.lower().endswith((".dvi", ".docx", ".doc")):
        title = _clean_title(raw_title)
    if not title and doc.page_count > 0:
        # Heuristic: first non-trivial line of page 1, often the title
        first_text = doc[0].get_text() or ""
        for line in first_text.splitlines():
            line = line.strip()
            if len(line) >= 10 and len(line) <= 150 and not line.isupper():
                title = _clean_title(line)
                break

    for page_num, page in enumerate(doc, start=1):
        text_parts.append(page.get_text())
        if len(images) >= MAX_IMAGES_PER_DOC:
            continue
        for img_info in page.get_images(full=True):
            if len(images) >= MAX_IMAGES_PER_DOC:
                break
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            raw = base.get("image")
            if not raw:
                continue
            h = hashlib.sha1(raw).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            ext = base.get("ext", "png")
            mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
            normalized = _normalize_image(raw, mime)
            if not normalized:
                continue
            data, mime, ext = normalized
            images.append({
                "data": data,
                "mime": mime,
                "ext": ext,
                "caption": f"Figure from page {page_num}",
                "source_url": "",  # populated by caller if relevant
                "source_kind": "pdf",
            })
    doc.close()

    text = "\n".join(text_parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"text": text[:30000], "images": images, "title": title}


def fetch_content(url, *, progress=None):
    """Dispatch to the right fetcher based on URL type.

    Performs URL canonicalization first (e.g. arxiv abstract → PDF), then
    a content-type sniff so PDFs served without a .pdf extension are still
    routed correctly.

    progress: optional callable(message: str) for status updates during slow
    YouTube frame extraction.
    """
    yt_id = extract_youtube_id(url)
    if yt_id:
        return fetch_youtube_content(url, yt_id, progress=progress), "youtube"

    canonical = canonicalize_url(url)
    if canonical != url:
        if progress:
            progress(f"Rewrote URL to: {canonical}")
        url = canonical

    if is_pdf_url(url):
        return fetch_pdf_content(url), "pdf"

    # Sniff content-type: some PDFs are served from URLs without .pdf
    try:
        resp = requests.head(url, timeout=10, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        ctype = resp.headers.get("content-type", "").lower()
        if "application/pdf" in ctype:
            return fetch_pdf_content(url), "pdf"
    except Exception:
        pass  # HEAD may fail; fall through to web fetch

    return fetch_web_content(url), "web"


# ── Guidelines loading ─────────────────────────────────────────────────────────

def load_guidelines():
    if not GUIDELINES_FILE.exists():
        print(f"Warning: {GUIDELINES_FILE.name} not found — using defaults.",
              file=sys.stderr)
        return ""
    raw = GUIDELINES_FILE.read_text()
    if "\n---\n" in raw:
        raw = raw.split("\n---\n", 1)[1]
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    return raw.strip()


# ── Card generation ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert at creating high-quality Anki flashcards.
When given content from an article, paper, or video transcript — together with
its figures and images — you produce concise, precise flashcards that follow
the user's guidelines exactly.

CRITICAL — VISUALS ARE A CORE PART OF THIS TASK:

1. Figures and images carry the heaviest signal. Study them FIRST and identify
   the core concept each one teaches BEFORE looking at the text.
2. When figures are provided, set `image_ref` AGGRESSIVELY. A card that could
   reasonably link to a figure SHOULD link to it — the visual reinforces recall.
   Err on the side of attaching figures, not omitting them.
3. Use text only to (a) clarify what the figures are showing and (b) cover
   important facts that no figure illustrates.
4. If NO figures are provided at all, the user has been warned that visuals
   couldn't be extracted — generate text-only cards but make them especially
   sharp, since the user may later use the "Enrich with visuals" button to
   attach figures per card.
"""

CARD_PROMPT_TEMPLATE = """\
Source URL: {url}
Source kind: {source_kind}

# Your guidelines
{guidelines}

# How to use the figures (above)
{figure_instructions}

# Task
Generate the appropriate number of Anki flashcards. **Let the content drive
the count.** A thin source might warrant 3–5 cards; a dense paper or
illustrated article might warrant 15–25; a passing thought might warrant 1.
Prioritize quality and selectivity. Skip anything not worth remembering.

# Card formats

1. **Basic** — front question / back answer:
   {{"type": "basic", "question": "...", "answer": "...", "image_ref": null}}

2. **Cloze** — sentence with key terms hidden using `{{{{c1::term}}}}`
   (use c1, c2, c3... for multiple deletions):
   {{"type": "cloze", "text": "...", "extra": "Optional back-side context.", "image_ref": null}}

**image_ref**: index (0, 1, 2, ...) of the figure this card relates to, or
`null` if no figure applies. Use the figure index as numbered in the
"figures" section. Cards that explain or annotate a figure SHOULD set
image_ref. Cards about purely textual facts should leave it null.

Return ONLY a JSON array — no explanation, no markdown fences:
[ {{...}}, {{...}}, ... ]

# Text content
{content}
"""

FIGURE_INSTRUCTIONS_WITH_IMAGES = """\
Above this prompt are {n} figure(s) from the source, in order (index 0, 1, …).
Their captions/alt text where available:

{caption_list}

Workflow:
1. Study each figure. What concept does it teach? What is its core insight?
2. Build cards FROM the figures first. Reference each by `image_ref`.
3. Then read the text and add cards for important facts not already covered.
4. Be selective — not every figure needs a card, and not every text fact does.
"""

FIGURE_INSTRUCTIONS_NO_IMAGES = """\
This source has no extractable figures (text-only or transcript). Work from
the text below; do not set `image_ref` on any card.
"""


def _build_figure_instructions(images):
    if not images:
        return FIGURE_INSTRUCTIONS_NO_IMAGES
    caption_lines = []
    for i, img in enumerate(images):
        cap = img.get("caption", "") or "(no caption)"
        caption_lines.append(f"  [{i}] {cap}")
    return FIGURE_INSTRUCTIONS_WITH_IMAGES.format(
        n=len(images),
        caption_list="\n".join(caption_lines),
    )


def _build_vision_content_blocks(images, text_prompt):
    """Build the list of content blocks for a Claude message: image blocks
    first (so Claude attends to them before reading instructions), then a
    single text block with the prompt + content."""
    blocks = []
    for img in images:
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["mime"],
                "data": base64.b64encode(img["data"]).decode("ascii"),
            },
        })
    blocks.append({"type": "text", "text": text_prompt})
    return blocks


def validate_card(card, n_images=None):
    """Validate and normalize a card dict. Returns the (mutated) card on
    success, or None if the card is malformed enough to drop.

    - Drops anything that isn't a dict.
    - Coerces `image_ref` to int (or None if non-numeric).
    - If `n_images` is given, clamps out-of-range `image_ref` to None.
    """
    if not isinstance(card, dict):
        return None
    ref = card.get("image_ref")
    if ref is not None:
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            ref = None
        else:
            if n_images is not None and not (0 <= ref < n_images):
                ref = None
    card["image_ref"] = ref
    return card


def validate_cards(cards, n_images=None):
    """Filter a list of cards through `validate_card`, dropping any that fail."""
    if not isinstance(cards, list):
        return []
    out = []
    for c in cards:
        v = validate_card(c, n_images=n_images)
        if v is not None:
            out.append(v)
    return out


def _parse_card_json(raw_text, n_images=None):
    raw = raw_text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    cards = json.loads(raw)
    return validate_cards(cards, n_images=n_images)


def generate_cards(content, url, guidelines, source_kind="web", model=None):
    """Generate flashcards from a content dict.

    content: {"text": str, "images": [{"data": bytes, "mime": str, ...}]}
    """
    client = anthropic.Anthropic()
    images = content.get("images", [])
    text = content.get("text", "")

    figure_instructions = _build_figure_instructions(images)
    text_prompt = CARD_PROMPT_TEMPLATE.format(
        url=url,
        source_kind=source_kind,
        guidelines=guidelines or "(No custom guidelines provided.)",
        figure_instructions=figure_instructions,
        content=text or "(no text extracted)",
    )

    blocks = _build_vision_content_blocks(images, text_prompt)
    resolved = resolve_model(model)
    print(f"[generate_cards] model={resolved} images={len(images)} text_chars={len(text)}", flush=True)
    message = client.messages.create(
        model=resolved,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": blocks}],
    )
    return _parse_card_json(message.content[0].text, n_images=len(images))


SUGGEST_PROMPT_TEMPLATE = """\
Source URL: {url}
Source kind: {source_kind}

# Your guidelines
{guidelines}

# How to use the figures (above)
{figure_instructions}

# Task
The user is manually adding flashcards to supplement the auto-generated set.
They've typed a concept/topic they want a card about:

    "{concept}"

Generate 1–3 candidate flashcards that target this concept, following the
guidelines above. If a figure is relevant to the concept, build the card
around it and set `image_ref`.

Rules:
- Each suggestion must be a complete, well-formed card.
- Do NOT duplicate any existing cards (listed below).
- If the concept isn't covered in the source, use general knowledge but stay grounded.

Existing cards in this draft (avoid duplicating):
{existing_summary}

Card formats (same as before, with `image_ref` index or `null`):
- Basic: {{"type": "basic", "question": "...", "answer": "...", "image_ref": null}}
- Cloze: {{"type": "cloze", "text": "...", "extra": "...", "image_ref": null}}

Return ONLY a JSON array — no explanation, no markdown fences:
[{{...}}, {{...}}]

# Text content
{content}
"""


def _summarize_existing(existing_cards):
    summary_lines = []
    for c in existing_cards:
        if c.get("type") == "cloze":
            summary_lines.append(f"- (cloze) {c.get('text', '')[:120]}")
        else:
            summary_lines.append(f"- (basic) Q: {c.get('question', '')[:120]}")
    return "\n".join(summary_lines) or "(none yet)"


def suggest_cards(content, url, guidelines, concept, existing_cards, source_kind="web", model=None):
    """Generate 1–3 candidate cards for a user-supplied concept."""
    client = anthropic.Anthropic()
    images = content.get("images", [])
    text = content.get("text", "")

    text_prompt = SUGGEST_PROMPT_TEMPLATE.format(
        url=url,
        source_kind=source_kind,
        guidelines=guidelines or "(No custom guidelines provided.)",
        figure_instructions=_build_figure_instructions(images),
        concept=concept,
        existing_summary=_summarize_existing(existing_cards),
        content=text or "(no text extracted)",
    )

    blocks = _build_vision_content_blocks(images, text_prompt)
    message = client.messages.create(
        model=resolve_model(model),
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": blocks}],
    )
    return _parse_card_json(message.content[0].text, n_images=len(images))


REROLL_PROMPT_TEMPLATE = """\
Source URL: {url}
Source kind: {source_kind}

# Your guidelines
{guidelines}

# How to use the figures (above)
{figure_instructions}

# Task
The user is rerolling a single existing card — they like the topic but want a
different angle, phrasing, or format. Generate ONE replacement card that:

- Covers the same concept as the original (described below)
- Differs meaningfully — different question framing, format (Basic vs Cloze),
  or focus aspect of the concept
- Doesn't duplicate any of the OTHER cards already in the draft
- Follows the guidelines above

Original card you're replacing:
{original}

Other cards in the draft (don't duplicate these):
{existing_summary}

Card formats:
- Basic: {{"type": "basic", "question": "...", "answer": "...", "image_ref": null}}
- Cloze: {{"type": "cloze", "text": "...", "extra": "...", "image_ref": null}}

Return ONLY a single JSON object — no array, no explanation, no markdown fences:
{{...}}

# Text content
{content}
"""


def _describe_card_concept(card):
    """A natural-language description of what concept a card is testing,
    used as a query when hunting for visuals."""
    if card.get("type") == "cloze":
        text = card.get("text", "")
        extra = card.get("extra", "")
        return f"Cloze: {text}\nContext: {extra}" if extra else f"Cloze: {text}"
    return f"Q: {card.get('question', '')}\nA: {card.get('answer', '')}"


def _format_transcript_indexed(segments):
    """Render transcript segments as '[N] [mm:ss] text' for segment-picking prompts."""
    lines = []
    for i, s in enumerate(segments):
        m, sec = divmod(int(s["start"]), 60)
        h, m = divmod(m, 60)
        ts = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        lines.append(f"[{i}] [{ts}] {s['text']}")
    return "\n".join(lines)


ENRICH_FIND_SEGMENT_PROMPT = """\
You're matching a flashcard to a YouTube video transcript. Each segment is
labeled with `[N]` (its index) and `[mm:ss]` (start time).

Find the SINGLE segment whose spoken content most directly discusses what
this flashcard is testing. Whatever visual is on screen at that moment is
what we'll attach to the card.

FLASHCARD:
{card_full}

TRANSCRIPT:
{transcript}

Pick the segment where the speaker is explicitly addressing the card's
specific concept — not generic intros, not adjacent topics, not sponsor reads.
If the topic appears multiple times, prefer the segment where it's EXPLAINED
(definition, mechanism, example) over where it's merely mentioned.

Return ONLY a JSON object — no commentary:
{{"segment_index": N, "reason": "tight one-sentence explanation"}}
"""

ENRICH_VISION_PICK_PROMPT = """\
You're picking the single best visual to attach to this flashcard:

CONCEPT:
{concept}

{n_frames} candidate frame(s) are above. For each, decide whether it would
genuinely strengthen the card. Return the index of the BEST one (a single
integer), or -1 if none are good enough.

Return ONLY a JSON object — no commentary:
{{"index": 0, "caption": "tight description of what's shown"}}
"""


def enrich_card_with_visuals(content, card, source_kind="web", model=None):
    """Hunt for a visual matching this card's concept.

    Returns (updated_card, new_image_or_None, message).
    - For YouTube: re-search transcript for relevant timestamps, extract those
      frames from the cached video, vision-pick the best, append to content.images.
    - For PDF/web: vision-rank the existing figures and re-assign image_ref to
      the best match. Does not extract new figures.
    """
    if not isinstance(card, dict):
        return card, None, "Invalid card"

    images = content.get("images", [])
    concept = _describe_card_concept(card)
    client = anthropic.Anthropic()
    resolved_model = resolve_model(model)

    # ── YouTube path: text-first segment match → frame at that moment ──
    if source_kind == "youtube" and content.get("_video_path") and content.get("_segments"):
        segments = content["_segments"]
        video_path = content["_video_path"]
        tmpdir = content.get("_video_tmpdir")
        if not (video_path and os.path.exists(video_path) and tmpdir):
            return card, None, "Video no longer cached; the draft may have expired."

        # Step 1: find the SINGLE transcript segment whose spoken content
        # most directly discusses this card.
        card_full = _describe_card_concept(card)
        indexed = _format_transcript_indexed(segments)[:28000]
        match_prompt = ENRICH_FIND_SEGMENT_PROMPT.format(
            card_full=card_full, transcript=indexed,
        )
        try:
            msg = client.messages.create(
                model=resolved_model, max_tokens=256,
                messages=[{"role": "user", "content": match_prompt}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            pick = json.loads(raw)
            seg_idx = int(pick.get("segment_index", -1))
            reason = (pick.get("reason") or "").strip()[:200]
        except Exception as e:
            return card, None, f"Could not match transcript: {e}"

        if not (0 <= seg_idx < len(segments)):
            return card, None, "Couldn't find a transcript segment matching this card."

        # Step 2: derive a timestamp from that segment. Use the END of the
        # segment (plus a small offset) — speakers typically introduce a
        # concept verbally and the visual is on screen by the time the
        # sentence completes. This avoids the off-by-N-seconds issue of
        # sampling at the segment start.
        segment = segments[seg_idx]
        next_segment = segments[seg_idx + 1] if seg_idx + 1 < len(segments) else None
        if next_segment:
            t = (segment["start"] + next_segment["start"]) / 2.0
        else:
            t = segment["start"] + 3.0  # final segment fallback

        # Step 3: extract one frame at that moment
        nonce = int(time.time() * 1000)
        frame_path = os.path.join(tmpdir, f"enrich_{nonce}.jpg")
        try:
            _extract_frame(video_path, t, frame_path)
            with open(frame_path, "rb") as fh:
                raw_bytes = fh.read()
        except Exception as e:
            return card, None, f"ffmpeg failed at t={t:.1f}s: {e}"
        normalized = _normalize_image(raw_bytes, "image/jpeg")
        if not normalized:
            return card, None, f"Extracted frame at t={t:.1f}s was unusable."
        data, mime, ext = normalized

        # Step 4: build the new image, append to draft, attach to card
        m, sec = divmod(int(t), 60)
        h, m = divmod(m, 60)
        ts_label = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        caption = f"At {ts_label} — {reason}" if reason else f"At {ts_label}"

        new_image = {
            "data": data, "mime": mime, "ext": ext,
            "caption": caption[:300],
            "source_url": content.get("_url", ""),
            "source_kind": "youtube",
        }
        new_idx = len(images)
        images.append(new_image)
        content["images"] = images
        card["image_ref"] = new_idx
        return card, new_image, f"Matched transcript [{ts_label}]: {reason}"

    # ── PDF / web path: vision-rank existing figures ──
    if not images:
        return card, None, "No figures available from this source to assign."

    blocks = []
    for img in images:
        data = img.get("data")
        if not isinstance(data, (bytes, bytearray)):
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img.get("mime", "image/jpeg"),
                       "data": base64.b64encode(data).decode("ascii")},
        })
    if not blocks:
        return card, None, "Figures are present but unusable."

    blocks.append({
        "type": "text",
        "text": ENRICH_VISION_PICK_PROMPT.format(concept=concept, n_frames=len(blocks)),
    })
    try:
        msg = client.messages.create(
            model=resolved_model, max_tokens=512,
            messages=[{"role": "user", "content": blocks}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        pick = json.loads(raw)
    except Exception as e:
        return card, None, f"Vision pick failed: {e}"

    idx = pick.get("index", -1)
    if not isinstance(idx, int) or idx < 0 or idx >= len(images):
        return card, None, "No existing figure was a strong match for this card."

    card["image_ref"] = idx
    return card, None, f"Assigned Fig {idx} to this card."


def reroll_card(content, url, guidelines, original_card, other_cards, source_kind="web", model=None):
    """Generate one replacement card on the same concept as `original_card`."""
    client = anthropic.Anthropic()
    images = content.get("images", [])
    text = content.get("text", "")

    if original_card.get("type") == "cloze":
        orig_desc = f'(cloze) text: "{original_card.get("text", "")}"'
        if original_card.get("extra"):
            orig_desc += f'\n  back extra: "{original_card.get("extra", "")}"'
    else:
        orig_desc = (
            f'(basic) Q: "{original_card.get("question", "")}"\n'
            f'  A: "{original_card.get("answer", "")}"'
        )
    if original_card.get("image_ref") is not None:
        orig_desc += f'\n  references figure {original_card["image_ref"]}'

    text_prompt = REROLL_PROMPT_TEMPLATE.format(
        url=url,
        source_kind=source_kind,
        guidelines=guidelines or "(No custom guidelines provided.)",
        figure_instructions=_build_figure_instructions(images),
        original=orig_desc,
        existing_summary=_summarize_existing(other_cards),
        content=text or "(no text extracted)",
    )
    blocks = _build_vision_content_blocks(images, text_prompt)
    message = client.messages.create(
        model=resolve_model(model),
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": blocks}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Reroll returns a single object, not an array
    card = json.loads(raw)
    if isinstance(card, list):  # be lenient if Claude returns an array
        card = card[0] if card else None
    return validate_card(card, n_images=len(images))


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Anki flashcards from a URL using Claude AI."
    )
    parser.add_argument("url", help="URL of the article, paper, or YouTube video")
    parser.add_argument(
        "--deck", default="AI Generated",
        help="Anki deck name (default: 'AI Generated')",
    )
    parser.add_argument(
        "--show-guidelines", action="store_true",
        help="Print the loaded guidelines and exit.",
    )
    args = parser.parse_args()

    guidelines = load_guidelines()
    if args.show_guidelines:
        print("── Loaded guidelines ──")
        print(guidelines or "(empty)")
        return

    print(f"\n[1/4] Fetching content from {args.url}")
    content, source_kind = fetch_content(args.url)
    n_imgs = len(content.get("images", []))
    n_chars = len(content.get("text", ""))
    print(f"      Source kind: {source_kind}")
    print(f"      Text: {n_chars:,} chars  |  Figures: {n_imgs}")

    if not content.get("text", "").strip() and not content.get("images"):
        print("Error: could not extract any usable content from that URL.",
              file=sys.stderr)
        sys.exit(1)

    print(f"\n[2/4] Generating flashcards with Claude (vision-enabled)...")
    cards = generate_cards(content, args.url, guidelines, source_kind=source_kind)
    n_basic = sum(1 for c in cards if c.get("type", "basic") == "basic")
    n_cloze = sum(1 for c in cards if c.get("type") == "cloze")
    n_with_img = sum(1 for c in cards if c.get("image_ref") is not None)
    print(f"      {len(cards)} cards generated "
          f"({n_basic} basic, {n_cloze} cloze, {n_with_img} with figure).")

    print(f"\n[3/4] Connecting to AnkiConnect...")
    try:
        ensure_deck(args.deck)
    except requests.ConnectionError:
        print(
            "\nError: cannot reach AnkiConnect at localhost:8765.\n"
            "  1. Make sure Anki is open.\n"
            "  2. Install AnkiConnect: Tools → Add-ons → Get Add-ons → code 2055492159\n"
            "  3. Restart Anki after installing.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n[4/4] Adding cards to '{args.deck}'...")
    added, skipped = add_cards_to_anki(cards, args.deck, images=content.get("images"))
    print(f"\nDone! {added} cards added, {skipped} duplicates skipped.\n")


if __name__ == "__main__":
    main()
