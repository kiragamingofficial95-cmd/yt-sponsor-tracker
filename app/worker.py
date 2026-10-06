"""24/7 endless worker:
1. EXPAND — every sweep, each tracked niche discovers MORE creators (rotating
   search queries, so 'tech' never stops yielding new channels).
2. DIG — creators with zero brands get their upload history paged back
   (25 videos/sweep) UNTIL the first brand is found (cap 300 videos).
3. FRESH — recent uploads checked for every creator on rotation.
Never 'done': the hunt only stops digging a creator once a brand is found.
"""
import asyncio
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import or_
from .db import SessionLocal, ensure_schema
from .models import Creator, Video, Sponsorship, Niche
from . import scraper, detector
from .config import settings

ensure_schema()

PIPE_VER = 5  # bump when detection improves -> brand-less videos get re-analyzed once

def norm(b: str) -> str:
    return b.strip().lower()

async def analyze_video(db: Session, creator: Creator, item: dict) -> int:
    vid = item["video_id"]
    v = db.query(Video).filter_by(video_id=vid).first()
    if v and v.analyzed and (v.pipe_ver or 0) >= PIPE_VER:
        return 0
    if not v:
        v = Video(video_id=vid, creator_id=creator.id, title=item.get("title", ""),
                  url=item.get("url", ""), published_at=item.get("published_at"))
        db.add(v); db.commit(); db.refresh(v)
    loop = asyncio.get_running_loop()
    det = await loop.run_in_executor(None, scraper.video_details, vid)
    tx = await loop.run_in_executor(None, scraper.get_transcript, vid)
    desc = det.get("description", "") or item.get("rss_desc", "")
    pinned = det.get("pinned_comment", "")
    v.duration_s = det.get("duration_s", 0)
    v.topic = det.get("topic", "") or creator.niche
    v.desc_len, v.tx_len = len(desc), len(tx)
    v.hint_score = detector.stage1_candidates(desc, pinned, tx)["score"]
    # v5 strict: drop legacy mention-only rows before re-detecting
    if (v.pipe_ver or 0) < PIPE_VER:
        db.query(Sponsorship).filter_by(video_id=v.id).delete()
        db.commit()
    v.pipe_ver = PIPE_VER
    hits = detector.detect(desc, pinned, tx,
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
    creator.total_checked = (creator.total_checked or 0) + 1
    if n > 0:
        creator.history_done = True  # brand found -> stop digging this creator
    db.commit()
    return n

async def expand_niches(db: Session) -> int:
    """One discovery variant per niche per sweep -> endless new creators."""
    added = 0
    loop = asyncio.get_running_loop()
    for niche in db.query(Niche).all():
        found = await loop.run_in_executor(
            None, scraper.discover_creators_for_niche, niche.name,
            settings.MAX_NEW_CREATORS_PER_SWEEP, niche.variant_cursor)
        niche.variant_cursor = (niche.variant_cursor or 0) + 1
        for f in found:
            if not db.query(Creator).filter_by(channel_id=f["channel_id"]).first():
                db.add(Creator(channel_id=f["channel_id"], name=f["name"],
                               url=f["url"], niche=niche.name))
                added += 1
                if added >= settings.MAX_NEW_CREATORS_PER_SWEEP:
                    break
        db.commit()
    return added

async def dig_histories() -> dict:
    """Page back through brand-less creators until first brand found."""
    stats = {"videos": 0, "brands": 0, "creators_dug": 0}
    db = SessionLocal()
    try:
        branded_ids = {r[0] for r in db.query(Video.creator_id).join(
            Sponsorship, Sponsorship.video_id == Video.id).distinct().all()}
        cands = db.query(Creator).filter(
            Creator.history_done == False,  # noqa
            ~Creator.id.in_(branded_ids) if branded_ids else True,
        ).order_by(Creator.last_checked.asc().nullsfirst()).limit(
            settings.HISTORY_CREATORS_PER_SWEEP).all()
        ids = [c.id for c in cands]
    finally:
        db.close()
    sem = asyncio.Semaphore(settings.MAX_WORKERS)
    loop = asyncio.get_running_loop()
    for cid in ids:
        d2 = SessionLocal()
        try:
            c = d2.query(Creator).filter_by(id=cid).first()
            if not c:
                continue
            known = {r[0] for r in d2.query(Video.video_id).filter_by(creator_id=cid).all()}
            items = await loop.run_in_executor(
                None, scraper.fetch_channel_history, c.channel_id, c.url,
                c.total_checked or 0, settings.HISTORY_VIDEOS_PER_SWEEP)
            items = [i for i in items if i["video_id"] not in known]
            if not items or (c.total_checked or 0) >= settings.HISTORY_MAX_VIDEOS:
                c.history_done = True  # exhausted or cap reached
                d2.commit()
                continue
            stats["creators_dug"] += 1
            async def handle(it):
                async with sem:
                    d3 = SessionLocal()
                    try:
                        cr = d3.query(Creator).filter_by(id=cid).first()
                        return (1, await analyze_video(d3, cr, it))
                    finally:
                        d3.close()
            for it in items:
                vv, bb = await handle(it)
                stats["videos"] += vv; stats["brands"] += bb
            c.last_checked = datetime.now(timezone.utc)
            d2.commit()
        finally:
            d2.close()
    return stats

async def sweep_recents() -> dict:
    stats = {"videos": 0, "brands": 0}
    db = SessionLocal()
    try:
        creators = db.query(Creator).order_by(
            Creator.last_checked.asc().nullsfirst()).limit(
            settings.RECENT_CREATORS_PER_SWEEP).all()
        ids = [c.id for c in creators]
    finally:
        db.close()
    sem = asyncio.Semaphore(settings.MAX_WORKERS)
    loop = asyncio.get_running_loop()
    for cid in ids:
        d2 = SessionLocal()
        try:
            c = d2.query(Creator).filter_by(id=cid).first()
            if not c:
                continue
            items = await loop.run_in_executor(
                None, scraper.fetch_rss_videos, c.channel_id, settings.VIDEOS_PER_CREATOR)
            c.last_checked = datetime.now(timezone.utc)
            d2.commit()
            async def handle(it):
                async with sem:
                    d3 = SessionLocal()
                    try:
                        cr = d3.query(Creator).filter_by(id=cid).first()
                        return (1, await analyze_video(d3, cr, it))
                    finally:
                        d3.close()
            for it in items:
                vv, bb = await handle(it)
                stats["videos"] += vv; stats["brands"] += bb
        except Exception:
            continue
        finally:
            d2.close()
    return stats

async def sweep_once() -> dict:
    db = SessionLocal()
    try:
        new_creators = await expand_niches(db)
    finally:
        db.close()
    rec = await sweep_recents()   # fast RSS path first — populates data quickly
    hist = await dig_histories()  # slow deep-dive second
    return {"new_creators": new_creators,
            "creators_dug": hist["creators_dug"],
            "videos": hist["videos"] + rec["videos"],
            "brands": hist["brands"] + rec["brands"]}

async def loop_forever():
    while True:
        try:
            s = await sweep_once()
            print(f"[worker] sweep done +{s['new_creators']} creators, "
                  f"dug {s['creators_dug']}, videos={s['videos']} new_brands={s['brands']}", flush=True)
        except Exception as e:
            print(f"[worker] error: {e}", flush=True)
        await asyncio.sleep(settings.SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(loop_forever())
