import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
import csv
import io
import os

from .db import SessionLocal, engine, get_db, ensure_schema
from .models import Base, Creator, Video, Sponsorship, Niche
from .config import settings
from . import scraper

ensure_schema()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.RUN_WORKER:
        from .worker import loop_forever
        task = asyncio.create_task(loop_forever())
    yield
    if task:
        task.cancel()

app = FastAPI(title="YT Sponsorship Analyzer 24/7")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def home():
    return FileResponse("static/dashboard.html")

@app.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    brands_24h = db.query(func.count(Sponsorship.id)).filter(
        Sponsorship.detected_at >= datetime.now(timezone.utc).replace(tzinfo=None) if False else Sponsorship.detected_at.isnot(None)).scalar()
    return {"creators": db.query(func.count(Creator.id)).scalar(),
            "videos": db.query(func.count(Video.id)).scalar(),
            "videos_analyzed": db.query(func.count(Video.id)).filter(Video.analyzed == True).scalar(),
            "sponsorships": db.query(func.count(Sponsorship.id)).scalar(),
            "brands": db.query(func.count(func.distinct(Sponsorship.brand_norm))).scalar()}

@app.get("/api/search")
def search(q: str = "", category: str = "", year_from: int = 0, year_to: int = 0,
           limit: int = 50, db: Session = Depends(get_db)):
    query = db.query(Sponsorship, Video, Creator).join(Video, Sponsorship.video_id == Video.id).join(
        Creator, Video.creator_id == Creator.id)
    if q:
        query = query.filter(Sponsorship.brand_norm.like(f"%{q.lower()}%"))
    if category:
        query = query.filter(Sponsorship.category == category)
    rows = query.order_by(desc(Sponsorship.detected_at)).limit(limit).all()
    out = []
    for s, v, c in rows:
        if year_from and v.published_at and v.published_at.year < year_from:
            continue
        if year_to and v.published_at and v.published_at.year > year_to:
            continue
        out.append({"brand": s.brand, "category": s.category, "confidence": s.confidence,
                    "evidence": s.evidence, "link": s.link, "method": s.method,
                    "video": v.title, "video_url": v.url,
                    "published": v.published_at.isoformat() if v.published_at else None,
                    "creator": c.name, "niche": c.niche, "topic": v.topic})
    return out


CSV_COLUMNS = ["brand", "category", "confidence", "method", "evidence", "link",
               "video_title", "video_url", "published", "creator", "creator_url",
               "niche", "topic", "detected_at"]


def _sponsorship_query(db: Session, q: str, category: str):
    query = db.query(Sponsorship, Video, Creator).join(
        Video, Sponsorship.video_id == Video.id).join(
        Creator, Video.creator_id == Creator.id)
    if q:
        query = query.filter(Sponsorship.brand_norm.like(f"%{q.lower()}%"))
    if category:
        query = query.filter(Sponsorship.category == category)
    return query.order_by(desc(Sponsorship.detected_at))


@app.get("/api/export.csv")
def export_csv(q: str = "", category: str = "", year_from: int = 0,
               year_to: int = 0, limit: int = 5000,
               db: Session = Depends(get_db)):
    """Bulk CSV export of sponsorships found — same filters as /api/search.

    Streams rows so large exports don't blow memory. Caps at 20000 rows.
    """
    limit = max(1, min(limit, 20000))

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_COLUMNS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        n = 0
        for s, v, c in _sponsorship_query(db, q, category).yield_per(500):
            if n >= limit:
                break
            if year_from and v.published_at and v.published_at.year < year_from:
                continue
            if year_to and v.published_at and v.published_at.year > year_to:
                continue
            w.writerow([s.brand, s.category, s.confidence, s.method,
                        s.evidence or "", s.link or "", v.title or "",
                        v.url or "",
                        v.published_at.isoformat() if v.published_at else "",
                        c.name or "", c.url or "", c.niche or "",
                        v.topic or "",
                        s.detected_at.isoformat() if s.detected_at else ""])
            n += 1
            if n % 200 == 0:  # flush in chunks, keep memory flat
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
        if buf.tell():
            yield buf.getvalue()

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition":
                                      "attachment; filename=sponsorships.csv"})

@app.get("/api/brand/{name}")
def brand_detail(name: str, db: Session = Depends(get_db)):
    norm = name.lower()
    rows = db.query(Sponsorship, Video, Creator).join(Video, Sponsorship.video_id == Video.id).join(
        Creator, Video.creator_id == Creator.id).filter(Sponsorship.brand_norm == norm).all()
    if not rows:
        # fuzzy fallback
        rows = db.query(Sponsorship, Video, Creator).join(Video, Sponsorship.video_id == Video.id).join(
            Creator, Video.creator_id == Creator.id).filter(Sponsorship.brand_norm.like(f"%{norm}%")).all()
    per_creator, per_topic, per_year = {}, {}, {}
    for s, v, c in rows:
        per_creator[c.name] = per_creator.get(c.name, 0) + 1
        per_topic[v.topic or c.niche or "unknown"] = per_topic.get(v.topic or c.niche or "unknown", 0) + 1
        y = v.published_at.year if v.published_at else (s.detected_at.year if s.detected_at else 0)
        per_year[str(y)] = per_year.get(str(y), 0) + 1
    vids = [{"video": v.title, "url": v.url, "creator": c.name,
             "published": v.published_at.isoformat() if v.published_at else None,
             "evidence": s.evidence, "category": s.category} for s, v, c in rows[:100]]
    return {"brand": name, "total": len(rows), "per_creator": per_creator,
            "per_topic": per_topic, "per_year": per_year, "videos": vids}

@app.get("/api/creators")
def list_creators(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "url": c.url, "niche": c.niche,
             "channel_id": c.channel_id} for c in db.query(Creator).all()]

@app.post("/api/creators")
def add_creator(channel: str, niche: str = "general", db: Session = Depends(get_db)):
    r = scraper.resolve_channel(channel)
    if not r:
        return {"error": "could not resolve channel — paste channel URL, UC id or @handle"}
    if db.query(Creator).filter_by(channel_id=r["channel_id"]).first():
        return {"ok": True, "dedup": True}
    c = Creator(channel_id=r["channel_id"], name=r["name"], url=r["url"], niche=niche)
    db.add(c); db.commit()
    return {"ok": True, "name": r["name"]}

@app.post("/api/niche")
def add_niche(niche: str, limit: int = 30, db: Session = Depends(get_db)):
    """Type 'tech SaaS' or 'healthcare' -> tracked forever: every sweep
    discovers MORE creators (rotating queries) and digs each one's history
    until a brand is found. Never stops at 10."""
    niche = niche.strip().lower()
    row = db.query(Niche).filter_by(name=niche).first()
    if not row:
        db.add(Niche(name=niche)); db.commit()
        row = db.query(Niche).filter_by(name=niche).first()
    found = scraper.discover_creators_for_niche(niche, limit, row.variant_cursor or 0)
    row.variant_cursor = (row.variant_cursor or 0) + 1
    added = 0
    for f in found:
        if not db.query(Creator).filter_by(channel_id=f["channel_id"]).first():
            db.add(Creator(channel_id=f["channel_id"], name=f["name"], url=f["url"], niche=niche))
            added += 1
    db.commit()
    return {"ok": True, "endless": True, "discovered": len(found), "added": added,
            "creators": found}

@app.get("/api/niches")
def niche_progress(db: Session = Depends(get_db)):
    """Per-niche hunt progress: tracked vs brand-found vs still digging."""
    out = []
    branded = {r[0] for r in db.query(Video.creator_id).join(
        Sponsorship, Sponsorship.video_id == Video.id).distinct().all()}
    for n in db.query(Niche).all():
        creators = db.query(Creator).filter_by(niche=n.name).all()
        with_brand = sum(1 for c in creators if c.id in branded or c.history_done)
        out.append({"niche": n.name, "creators": len(creators),
                    "with_brand_or_done": with_brand,
                    "still_digging": len(creators) - with_brand,
                    "videos_checked": sum(c.total_checked or 0 for c in creators)})
    return out

@app.post("/api/scan")
async def trigger_scan():
    from .worker import sweep_once
    return await sweep_once()

@app.post("/api/scan-one")
async def scan_one(video: str, db: Session = Depends(get_db)):
    """Analyze ONE video synchronously and return exactly what was seen.
    Definitive per-video diagnosis: pass video id or watch URL."""
    import re
    from .worker import analyze_video
    m = re.search(r"([\w-]{11})", video)
    if not m:
        return {"error": "pass a video id or watch URL"}
    vid = m.group(1)
    c = db.query(Creator).first()
    if not c:
        return {"error": "add a creator/niche first"}
    # force fresh analysis
    v = db.query(Video).filter_by(video_id=vid).first()
    if v:
        v.analyzed = False; v.pipe_ver = 0; db.commit()
    item = {"video_id": vid, "title": "", "url": f"https://www.youtube.com/watch?v={vid}",
            "published_at": None, "rss_desc": ""}
    await analyze_video(db, c, item)
    v = db.query(Video).filter_by(video_id=vid).first()
    spons = [{"brand": s.brand, "method": s.method, "evidence": (s.evidence or "")[:200]}
             for s in db.query(Sponsorship).filter_by(video_id=v.id).all()]
    return {"video_id": vid, "desc_len": v.desc_len, "tx_len": v.tx_len,
            "hint_score": v.hint_score, "sponsorships": spons}

@app.post("/api/admin/prune-legacy")
def prune_legacy(db: Session = Depends(get_db)):
    """One-off: delete v4 mention-based rows (conf<0.7) + reopen falsely-closed digs."""
    old = db.query(Sponsorship).filter(Sponsorship.confidence < 0.7).count()
    db.query(Sponsorship).filter(Sponsorship.confidence < 0.7).delete(synchronize_session=False)
    db.commit()
    branded = {r[0] for r in db.query(Video.creator_id).join(
        Sponsorship, Sponsorship.video_id == Video.id).distinct().all()}
    q = db.query(Creator) if not branded else db.query(Creator).filter(~Creator.id.in_(branded))
    reset = q.update({Creator.history_done: False}, synchronize_session=False)
    db.commit()
    return {"deleted_legacy": old, "reopened_creators": reset}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/debug/videos")
def debug_videos(limit: int = 20, db: Session = Depends(get_db)):
    rows = db.query(Video, Creator).join(Creator, Video.creator_id == Creator.id).order_by(
        Video.id.desc()).limit(limit).all()
    out = []
    for v, c in rows:
        n = db.query(Sponsorship).filter_by(video_id=v.id).count()
        out.append({"video": (v.title or "")[:70], "creator": c.name,
                    "desc_len": v.desc_len or 0, "tx_len": v.tx_len or 0,
                    "hint_score": v.hint_score or 0, "sponsorships": n})
    return out

@app.get("/api/debug/rss")
def debug_rss(channel: str):
    """What does the SERVER see in a channel RSS feed? (keys + summary lens)"""
    import feedparser
    f = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}")
    items = []
    for e in f.entries[:5]:
        items.append({"id": e.get("yt_videoid", ""),
                      "summary_len": len(e.get("summary") or ""),
                      "summary_head": (e.get("summary") or "")[:120]})
    return {"entries": len(f.entries), "items": items,
            "bozo": bool(getattr(f, "bozo", False))}

@app.get("/api/debug/ytdlp")
def debug_ytdlp(video: str):
    """What does the SERVER get from yt-dlp for one video? (keys + lens)"""
    from . import scraper
    info = scraper.ytdlp_info(f"https://www.youtube.com/watch?v={video}")
    if not info:
        return {"ok": False}
    return {"ok": True, "desc_len": len(info.get("description") or ""),
            "comments": len(info.get("comments") or []),
            "has_duration": bool(info.get("duration")),
            "channel": info.get("channel", "")[:60]}
