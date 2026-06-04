"""Non-file knowledge sources → text → existing graph pipeline.

Three sources, all reduced to plain text so the SAME (consistent) embedding
pipeline turns them into GraphRAG — we never mix embedding spaces:

  • URL      — server-side fetch + readable-text extraction
  • YouTube  — transcript via youtube-transcript-api (no API key needed)
  • Media    — audio/video transcribed by Gemini (your Google API key)

Each returns (title, text); the caller writes a temp .txt and feeds it to
DecisionGraph.ingest() / CompanyMemory.ingest().
"""
from __future__ import annotations
import re, os, json, tempfile


# ── URL → text ────────────────────────────────────────────────────────────────
def fetch_url_text(url: str, max_chars: int = 200_000) -> tuple[str, str]:
    import requests
    from bs4 import BeautifulSoup
    if not re.match(r"^https?://", url):
        url = "https://" + url
    r = requests.get(url, timeout=25, headers={
        "User-Agent": "Mozilla/5.0 (DecisionGraph ingest bot)"
    })
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer",
                     "nav", "aside", "form", "svg"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string
             else url)
    # prefer main/article if present
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n",
                  re.sub(r"[ \t]+", " ", main.get_text("\n"))).strip()
    if len(text) < 80:
        raise ValueError("page had almost no extractable text "
                         "(likely a JS-rendered SPA — not supported)")
    return title, f"# {title}\nSource: {url}\n\n{text[:max_chars]}"


# ── YouTube → transcript ──────────────────────────────────────────────────────
def _yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else url.strip()

def fetch_youtube_transcript(url: str) -> tuple[str, str]:
    from youtube_transcript_api import YouTubeTranscriptApi
    vid = _yt_id(url)
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(vid)
        chunks = [s.text for s in fetched]
    except Exception:
        # older/newer API shape fallback
        tr = YouTubeTranscriptApi.get_transcript(vid)  # type: ignore
        chunks = [c["text"] for c in tr]
    text = " ".join(c.strip() for c in chunks if c.strip())
    if len(text) < 40:
        raise ValueError("no usable transcript (video may have captions disabled)")
    title = f"YouTube {vid}"
    return title, f"# {title}\nSource: https://youtu.be/{vid}\n\nTranscript:\n{text}"


# ── Media (audio/video) → transcript via Gemini ───────────────────────────────
def transcribe_media_gemini(file_path: str, api_key: str,
                            model: str = "gemini-2.0-flash") -> tuple[str, str]:
    import google.generativeai as genai
    if not api_key:
        raise ValueError("Gemini API key not configured on server")
    genai.configure(api_key=api_key)
    f = genai.upload_file(path=file_path)
    # wait for processing
    import time
    for _ in range(60):
        f = genai.get_file(f.name)
        if f.state.name == "ACTIVE":
            break
        if f.state.name == "FAILED":
            raise RuntimeError("Gemini file processing failed")
        time.sleep(2)
    m = genai.GenerativeModel(model)
    resp = m.generate_content([
        "Transcribe this media fully and accurately. Then add a short "
        "'Key points:' section listing the main topics/entities discussed. "
        "Plain text only.",
        f,
    ])
    text = (resp.text or "").strip()
    try: genai.delete_file(f.name)
    except Exception: pass
    if len(text) < 40:
        raise ValueError("transcription returned almost nothing")
    base = os.path.basename(file_path)
    return f"Media: {base}", f"# Transcript of {base}\n\n{text}"


# ── helper: write text to a temp .txt so existing ingest() can consume it ─────
def text_to_tempfile(text: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                      delete=False, encoding="utf-8")
    tmp.write(text); tmp.close()
    return tmp.name
