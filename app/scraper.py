"""Quota-free YouTube scraping: RSS + yt-dlp (no API key needed).
Covers: niche->creators discovery, creator->videos, video description + pinned comment.
Anti-429: proxy rotation (PROXY_URLS), request throttle, retries, and
third-party fallbacks (Piped/Invidious fetch from THEIR servers, not ours).
"""
import re, feedparser, httpx, itertools, threading, time, random
from datetime import datetime, timezone
from typing import List, Dict, Optional

RSS_TMPL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

# --- proxy rotation + throttle -------------------------------------------
_lock = threading.Lock()
_proxy_cycle = None
_last_call = 0.0


def _proxy_list() -> List[str]:
    try:
        from .config import settings
        raw = (settings.PROXY_URLS or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _next_proxy() -> Optional[str]:
    global _proxy_cycle
    proxies = _proxy_list()
    if not proxies:
        return None
    with _lock:
        if _proxy_cycle is None:
            _proxy_cycle = itertools.cycle(proxies)
        return next(_proxy_cycle)


def _throttle():
    try:
        from .config import settings
        gap = float(settings.YT_MIN_INTERVAL_SEC or 0)
    except Exception:
        gap = 0
    if gap <= 0:
        return
    global _last_call
    with _lock:
        now = time.monotonic()
        wait = gap - (now - _last_call)
        if wait > 0:
            time.sleep(wait + random.uniform(0, gap * 0.5))
        _last_call = time.monotonic()


def _http_client(timeout: float = 12) -> httpx.Client:
    px = _next_proxy()
    if px:
        return httpx.Client(timeout=timeout, proxy=px)
    return httpx.Client(timeout=timeout)

SPONSOR_HINTS = re.compile(
    r"(sponsor|thanks? to|partnered with|use code|discount|link in (the )?description|"
    r"affiliate|#ad\b|\bad\b|paid promotion|brand deal|gifted|promo code)",
    re.I,
)

def fetch_rss_videos(channel_id: str, limit: int = 8) -> List[Dict]:
    try:
        # fetch via rotating proxy so one IP doesn't eat all RSS 429s,
        # fall back to direct feedparser on failure
        xml = ""
        try:
            _throttle()
            with _http_client(timeout=15) as c:
                r = c.get(RSS_TMPL.format(cid=channel_id),
                          headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and "<entry" in r.text:
                    xml = r.text
        except Exception:
            xml = ""
        feed = feedparser.parse(xml) if xml else feedparser.parse(
            RSS_TMPL.format(cid=channel_id))
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

CLIENT_SETS = [
    ["android", "ios", "web"],
    ["ios", "android", "web"],
    ["web", "android", "ios"],
    ["tv", "android"],
]

def _ytdlp_attempt(url: str, extra: dict, proxy: Optional[str],
                   clients: List[str]) -> Optional[Dict]:
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "skip_download": True,
            "extractor_args": {"youtube": {"player_client": clients}},
            "socket_timeout": 15, "retries": 2, "fragment_retries": 2,
            "nocheckcertificate": True}
    if proxy:
        opts["proxy"] = proxy
    opts.update(extra)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def ytdlp_info(url: str, extra_opts: Optional[dict] = None) -> Optional[Dict]:
    """Single video or channel metadata via yt-dlp. Returns None on failure.
    Rotates proxies + player clients with throttle + backoff so a 429 on one
    identity doesn't kill the whole sweep."""
    try:
        from .config import settings
        tries = max(1, int(settings.YT_RETRIES or 1))
    except Exception:
        tries = 3
    extra_opts = extra_opts or {}
    last = None
    for i in range(tries):
        _throttle()
        try:
            return _ytdlp_attempt(
                url, {**{"getcomments": True,
                         "extractor_args": {"youtube": {
                             "player_client": CLIENT_SETS[i % len(CLIENT_SETS)],
                             "max_comments": ["20", "20", "0", "0"]}}},
                       **extra_opts},
                _next_proxy(), CLIENT_SETS[i % len(CLIENT_SETS)])
        except Exception as e:
            last = e
            time.sleep(min(8, 0.7 * (2 ** i)) + random.uniform(0, 0.5))
            continue
    return None


def _ytdlp_flat(url: str, flat_opts: dict) -> Optional[Dict]:
    try:
        from .config import settings
        tries = max(1, int(settings.YT_RETRIES or 1))
    except Exception:
        tries = 3
    for i in range(tries):
        _throttle()
        try:
            return _ytdlp_attempt(url, {"extract_flat": True, **flat_opts},
                                  _next_proxy(), CLIENT_SETS[i % len(CLIENT_SETS)])
        except Exception:
            time.sleep(min(8, 0.7 * (2 ** i)) + random.uniform(0, 0.5))
    return None

def video_details(video_id: str) -> Dict:
    """Description + pinned/top comment text — the sponsorship surface.
    Falls back to Piped/Invidious (their servers, not our IP) when yt-dlp
    is rate-limited, so a 429 never means a blind analysis."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = ytdlp_info(url)
    if not info:
        desc = piped_description(video_id) or invidious_description(video_id)
        return {"description": desc, "pinned_comment": "", "duration_s": 0,
                "channel_id": "", "channel": "", "topic": ""}
    desc = info.get("description", "") or ""
    if not desc:
        desc = piped_description(video_id) or invidious_description(video_id)
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
    for q in ordered:
        data = _ytdlp_flat(f"ytsearch{limit}:{q}", {"socket_timeout": 25})
        if not data:
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
    return out

def fetch_channel_history(channel_id: str, channel_url: str, skip: int = 0,
                          batch: int = 25) -> List[Dict]:
    """Page BACK through a channel's uploads (oldest hunt). yt-dlp flat playlist
    with playliststart/end — no API key. Returns items not yet checked."""
    url = channel_url or f"https://www.youtube.com/channel/{channel_id}/videos"
    if "/videos" not in url:
        url = url.rstrip("/") + "/videos"
    data = _ytdlp_flat(url, {"playliststart": skip + 1,
                             "playlistend": skip + batch,
                             "socket_timeout": 30})
    if not data:
        return []
    out = []
    for e in (data.get("entries") or []):
        vid = e.get("id", "")
        if not vid or len(vid) > 16:
            continue
        out.append({"video_id": vid, "title": e.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "published_at": datetime.now(timezone.utc)})
    return out

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
    "https://pipedapi.reallyaweso.me",
    "https://api-piped.mha.fi",
    "https://pipedapi.syncpundit.io",
    "https://pipedapi.r4fo.com",
    "https://pipedapi.nosebs.ru",
]

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://iv.melmac.space",
    "https://invidious.reallyaweso.me",
    "https://vid.puffyan.us",
    "https://inv.tux.pizza",
]

def piped_description(video_id: str) -> str:
    """Piped API fallback for description when YouTube blocks the server IP."""
    for base in PIPED_INSTANCES:
        try:
            _throttle()
            with _http_client(timeout=12) as c:
                r = c.get(f"{base}/streams/{video_id}")
                if r.status_code == 200:
                    d = (r.json().get("description") or "")[:8000]
                    if d.strip():
                        return d
        except Exception:
            continue
    return ""

def invidious_description(video_id: str) -> str:
    """Invidious API fallback — their server fetches from YouTube, not ours."""
    for base in INVIDIOUS_INSTANCES:
        try:
            _throttle()
            with _http_client(timeout=12) as c:
                r = c.get(f"{base}/api/v1/videos/{video_id}")
                if r.status_code == 200:
                    d = (r.json().get("description") or "")[:8000]
                    if d.strip():
                        return d
        except Exception:
            continue
    return ""

def invidious_transcript(video_id: str, max_chars: int = 12000) -> str:
    """Captions via Invidious (third-party IP). Tries en auto/manual tracks."""
    for base in INVIDIOUS_INSTANCES:
        try:
            _throttle()
            with _http_client(timeout=12) as c:
                r = c.get(f"{base}/api/v1/captions/{video_id}")
                if r.status_code != 200:
                    continue
                caps = r.json().get("captions") or []
                en = [t for t in caps if (t.get("languageCode") or "").startswith("en")]
                track = (en or caps[:1] or [None])[0]
                if not track or not track.get("url"):
                    continue
                u = track["url"]
                if u.startswith("/"):
                    u = base + u
                s = c.get(u + ("&tlang=en" if "tlang" not in u else ""))
                if s.status_code != 200 or not s.text.strip():
                    continue
                lines = [l.strip() for l in s.text.splitlines()
                         if l.strip() and "-->" not in l
                         and not l.strip()[0].isdigit()
                         and not l.startswith("WEBVTT")
                         and not l.startswith("Kind:")
                         and not l.startswith("Language:")]
                seen, out = set(), []
                for l in lines:
                    if l not in seen:
                        seen.add(l)
                        out.append(l)
                txt = " ".join(out)[:max_chars]
                if txt.strip():
                    return txt
        except Exception:
            continue
    return ""

def get_transcript(video_id: str, max_chars: int = 12000) -> str:
    """Primary captions API -> Invidious (third-party IP) -> yt-dlp auto-subs."""
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
    tx = invidious_transcript(video_id, max_chars)
    if tx.strip():
        return tx
    return subs_via_ytdlp(video_id, max_chars)

def subs_via_ytdlp(video_id: str, max_chars: int = 12000) -> str:
    """Auto-caption fallback via yt-dlp (timedtext endpoint often unblocked)."""
    import os, glob, tempfile
    try:
        from yt_dlp import YoutubeDL
        tmp = tempfile.mkdtemp()
        _throttle()
        opts = {"quiet": True, "skip_download": True, "writeautomaticsub": True,
                "subtitleslangs": ["en"], "subtitlesformat": "vtt",
                "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
                "extractor_args": {"youtube": {"player_client": ["android", "ios"]}},
                "socket_timeout": 15, "retries": 2}
        px = _next_proxy()
        if px:
            opts["proxy"] = px
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
