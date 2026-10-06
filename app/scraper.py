"""Quota-free YouTube scraping: RSS + yt-dlp (no API key needed).
Covers: niche->creators discovery, creator->videos, video description + pinned comment.
"""
import re, feedparser, httpx
from datetime import datetime, timezone
from typing import List, Dict, Optional

RSS_TMPL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

SPONSOR_HINTS = re.compile(
    r"(sponsor|thanks? to|partnered with|use code|discount|link in (the )?description|"
    r"affiliate|#ad\b|\bad\b|paid promotion|brand deal|gifted|promo code)",
    re.I,
)

def fetch_rss_videos(channel_id: str, limit: int = 8) -> List[Dict]:
    try:
        feed = feedparser.parse(RSS_TMPL.format(cid=channel_id))
        out = []
        for e in feed.entries[:limit]:
            vid = e.get("yt_videoid", "")
            if not vid and "link" in e:
                m = re.search(r"v=([\w-]{11})", e.link)
                vid = m.group(1) if m else ""
            if not vid:
                continue
            pub = e.get("published_parsed")
            published = datetime(*pub[:6], tzinfo=timezone.utc) if pub else datetime.now(timezone.utc)
            # RSS sometimes carries the description — fallback if yt-dlp is blocked
            rss_desc = (e.get("summary") or e.get("media_description") or "")[:4000]
            out.append({"video_id": vid, "title": e.get("title", ""),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "published_at": published, "rss_desc": rss_desc})
        return out
    except Exception:
        return []

def ytdlp_info(url: str) -> Optional[Dict]:
    """Single video or channel metadata via yt-dlp. Returns None on failure.
    Uses mobile player clients — they bypass YouTube's datacenter bot-check
    that blocks plain webpage extraction on servers like Railway."""
    try:
        from yt_dlp import YoutubeDL
        opts = {"quiet": True, "skip_download": True, "getcomments": True,
                "extractor_args": {"youtube": {
                    "player_client": ["android", "ios", "web"],
                    "max_comments": ["20", "20", "0", "0"]}},
                "socket_timeout": 10}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None

def video_details(video_id: str) -> Dict:
    """Description + pinned/top comment text — the sponsorship surface."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = ytdlp_info(url)
    if not info:
        return {"description": "", "pinned_comment": "", "duration_s": 0,
                "channel_id": "", "channel": "", "topic": ""}
    desc = info.get("description", "") or ""
    pinned, top = "", ""
    for c in (info.get("comments") or [])[:20]:
        txt = (c.get("text") or "")[:2000]
        author = (c.get("author") or "")
        if c.get("is_pinned"):
            pinned = f"[{author}]: {txt}"
            break
        if not top and author and info.get("channel", "") in author:
            top = f"[{author}]: {txt}"
    return {"description": desc[:8000], "pinned_comment": pinned or top,
            "duration_s": int(info.get("duration") or 0),
            "channel_id": info.get("channel_id", "") or "",
            "channel": info.get("channel", "") or "",
            "topic": (info.get("categories") or [""])[0] if info.get("categories") else ""}

DISCOVERY_TEMPLATES = [
    "{n} youtuber",
    "best {n} youtube channels",
    "{n} review channel",
    "{n} explained",
    "{n} podcast",
    "{n} news channel",
    "{n} tutorial youtuber",
    "{n} vlog channel",
]

def discover_creators_for_niche(niche: str, limit: int = 30, variant: int = 0) -> List[Dict]:
    """Quota-free ytsearch discovery. `variant` rotates the query template each
    sweep so the same niche keeps yielding NEW creators instead of the same 10."""
    queries = [t.format(n=niche) for t in DISCOVERY_TEMPLATES]
    # rotate: start at cursor, try up to 3 templates per call
    ordered = [queries[(variant + i) % len(queries)] for i in range(3)]
    seen, out = set(), []
    try:
        from yt_dlp import YoutubeDL
        opts = {"quiet": True, "skip_download": True, "extract_flat": True, "socket_timeout": 25}
        with YoutubeDL(opts) as ydl:
            for q in ordered:
                try:
                    data = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
                except Exception:
                    continue
                for e in (data.get("entries") or []):
                    cid = e.get("channel_id") or e.get("id") or ""
                    ch = e.get("channel") or e.get("uploader") or ""
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    out.append({"channel_id": cid, "name": ch,
                                "url": e.get("channel_url") or e.get("url") or "",
                                "niche": niche})
                    if len(out) >= limit:
                        return out
    except Exception:
        pass
    return out

def fetch_channel_history(channel_id: str, channel_url: str, skip: int = 0,
                          batch: int = 25) -> List[Dict]:
    """Page BACK through a channel's uploads (oldest hunt). yt-dlp flat playlist
    with playliststart/end — no API key. Returns items not yet checked."""
    try:
        from yt_dlp import YoutubeDL
        url = channel_url or f"https://www.youtube.com/channel/{channel_id}/videos"
        if "/videos" not in url:
            url = url.rstrip("/") + "/videos"
        opts = {"quiet": True, "skip_download": True, "extract_flat": True,
                "playliststart": skip + 1, "playlistend": skip + batch,
                "socket_timeout": 30}
        with YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        out = []
        for e in (data.get("entries") or []):
            vid = e.get("id", "")
            if not vid or len(vid) > 16:
                continue
            out.append({"video_id": vid, "title": e.get("title", ""),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "published_at": datetime.now(timezone.utc)})
        return out
    except Exception:
        return []

def resolve_channel(query: str) -> Optional[Dict]:
    """Accept channel_id (UC...), handle (@name), or URL -> {channel_id,name,url}."""
    query = query.strip()
    m = re.search(r"(UC[\w-]{22})", query)
    if m:
        info = ytdlp_info(f"https://www.youtube.com/channel/{m.group(1)}")
        if info:
            return {"channel_id": m.group(1), "name": info.get("channel") or m.group(1),
                    "url": f"https://www.youtube.com/channel/{m.group(1)}"}
        return {"channel_id": m.group(1), "name": m.group(1),
                "url": f"https://www.youtube.com/channel/{m.group(1)}"}
    handle = query if query.startswith("@") else None
    if "/" in query or "youtube.com" in query:
        info = ytdlp_info(query)
        if info and info.get("channel_id"):
            return {"channel_id": info["channel_id"], "name": info.get("channel", query), "url": query}
    info = ytdlp_info(f"https://www.youtube.com/{handle or query}")
    if info and info.get("channel_id"):
        return {"channel_id": info["channel_id"], "name": info.get("channel", query),
                "url": info.get("channel_url", query)}
    return None

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.leptons.xyz",
]

def piped_description(video_id: str) -> str:
    """Piped API fallback for description when YouTube blocks the server IP."""
    for base in PIPED_INSTANCES:
        try:
            with httpx.Client(timeout=12) as c:
                r = c.get(f"{base}/streams/{video_id}")
                if r.status_code == 200:
                    return (r.json().get("description") or "")[:8000]
        except Exception:
            continue
    return ""

def get_transcript(video_id: str, max_chars: int = 12000) -> str:
    """Primary captions API -> yt-dlp auto-subs fallback."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        segs = YouTubeTranscriptApi.get_transcript(video_id)
        # sponsor segments usually front/mid — sample head + middle
        n = len(segs)
        pick = segs[:60] + (segs[n//2-15:n//2+15] if n > 120 else [])
        text = " ".join(s.get("text", "") for s in pick)
        if text.strip():
            return text[:max_chars]
    except Exception:
        pass
    return subs_via_ytdlp(video_id, max_chars)

def subs_via_ytdlp(video_id: str, max_chars: int = 12000) -> str:
    """Auto-caption fallback via yt-dlp (timedtext endpoint often unblocked)."""
    import os, glob, tempfile
    try:
        from yt_dlp import YoutubeDL
        tmp = tempfile.mkdtemp()
        opts = {"quiet": True, "skip_download": True, "writeautomaticsub": True,
                "subtitleslangs": ["en"], "subtitlesformat": "vtt",
                "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
                "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
                "socket_timeout": 15}
        with YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        files = glob.glob(os.path.join(tmp, "*.vtt"))
        if not files:
            return ""
        with open(files[0], encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f if l.strip() and "-->" not in l
                     and not l.strip()[0].isdigit() and not l.startswith("WEBVTT")]
        seen, out = set(), []
        for l in lines:
            if l not in seen:
                seen.add(l); out.append(l)
        return " ".join(out)[:max_chars]
    except Exception:
        return ""
