"""24/7 worker: sweep creators -> recent videos -> detect sponsorships. Fast + continuous."""
import asyncio, time
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from .db import SessionLocal, engine
from .models import Base, Creator, Video, Sponsorship
from . import scraper, detector
from .config import settings

Base.metadata.create_all(bind=engine)

def norm(b: str) -> str:
    return b.strip().lower()

async def analyze_video(db: Session, creator: Creator, item: dict) -> int:
    vid = item["video_id"]
    v = db.query(Video).filter_by(video_id=vid).first()
    if v and v.analyzed:
        return 0
    if not v:
        v = Video(video_id=vid, creator_id=creator.id, title=item.get("title", ""),
                  url=item.get("url", ""), published_at=item.get("published_at"))
        db.add(v); db.commit(); db.refresh(v)
    loop = asyncio.get_running_loop()
    det = await loop.run_in_executor(None, scraper.video_details, vid)
    tx = await loop.run_in_executor(None, scraper.get_transcript, vid)
    v.duration_s = det.get("duration_s", 0)
    v.topic = det.get("topic", "") or creator.niche
    if det.get("channel_id") and not creator.channel_id.startswith("UC"):
        pass
    hits = detector.detect(det.get("description", ""), det.get("pinned_comment", ""), tx,
                           settings.GROQ_API_KEY, settings.GROQ_MODEL)
    n = 0
    for h in hits:
        bnorm = norm(h["brand"])
        if db.query(Sponsorship).filter_by(video_id=v.id, brand_norm=bnorm).first():
            continue
        db.add(Sponsorship(video_id=v.id, brand=h["brand"][:255], brand_norm=bnorm,
                           category=h.get("category", "Other"), confidence=h.get("confidence", 0.5),
                           evidence=h.get("evidence", "")[:2000], link=h.get("link", "")[:1024],
                           method=h.get("method", "regex")))
        n += 1
    v.analyzed = True
    v.analyzed_at = datetime.now(timezone.utc)
    db.commit()
    return n

async def sweep_once() -> dict:
    db = SessionLocal()
    stats = {"videos": 0, "brands": 0}
    try:
        creators = db.query(Creator).all()
        sem = asyncio.Semaphore(settings.MAX_WORKERS)
        async def handle_creator(c: Creator):
            items = await asyncio.get_running_loop().run_in_executor(
                None, scraper.fetch_rss_videos, c.channel_id, settings.VIDEOS_PER_CREATOR)
            # RSS empty (new handle?) -> try channel videos page via yt-dlp flat
            if not items:
                def _flat():
                    try:
                        from yt_dlp import YoutubeDL
                        with YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": settings.VIDEOS_PER_CREATOR}) as y:
                            d = y.extract_info(c.url, download=False)
                        entries = (d.get("entries") or [])[:settings.VIDEOS_PER_CREATOR]
                        return [{"video_id": e.get("id", ""), "title": e.get("title", ""),
                                 "url": f"https://www.youtube.com/watch?v={e.get('id','')}",
                                 "published_at": datetime.now(timezone.utc)} for e in entries if e.get("id")]
                    except Exception:
                        return []
                items = await asyncio.get_running_loop().run_in_executor(None, _flat)
            c.last_checked = datetime.now(timezone.utc)
            db.commit()
            async def handle_item(it):
                async with sem:
                    d2 = SessionLocal()
                    try:
                        cr = d2.query(Creator).filter_by(id=c.id).first()
                        br = await analyze_video(d2, cr, it)
                        return (1, br)
                    finally:
                        d2.close()
            res = await asyncio.gather(*[handle_item(i) for i in items])
            return res
        for c in creators:
            try:
                res = await handle_creator(c)
                for vv, bb in res:
                    stats["videos"] += vv; stats["brands"] += bb
            except Exception:
                continue
        return stats
    finally:
        db.close()

async def loop_forever():
    while True:
        try:
            s = await sweep_once()
            print(f"[worker] sweep done videos={s['videos']} new_brands={s['brands']}", flush=True)
        except Exception as e:
            print(f"[worker] error: {e}", flush=True)
        await asyncio.sleep(settings.SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(loop_forever())
