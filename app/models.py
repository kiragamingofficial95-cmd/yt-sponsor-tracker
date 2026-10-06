from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .db import Base

def now():
    return datetime.now(timezone.utc)

class Creator(Base):
    __tablename__ = "creators"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(64), unique=True, index=True)  # UC... or handle
    name = Column(String(255), index=True)
    url = Column(String(512))
    niche = Column(String(128), index=True, default="general")
    subs = Column(Integer, default=0)
    last_checked = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    # endless-hunt tracking: keep digging history until first brand found
    history_done = Column(Boolean, default=False, index=True)
    total_checked = Column(Integer, default=0)
    videos = relationship("Video", back_populates="creator", cascade="all,delete")

class Niche(Base):
    """A hunt the user started by typing e.g. 'tech'. Worker expands it forever."""
    __tablename__ = "niches"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True)
    variant_cursor = Column(Integer, default=0)  # rotates discovery queries
    created_at = Column(DateTime(timezone=True), default=now)

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True)
    video_id = Column(String(32), unique=True, index=True)
    creator_id = Column(Integer, ForeignKey("creators.id"), index=True)
    title = Column(Text)
    url = Column(String(512))
    published_at = Column(DateTime(timezone=True), index=True)
    duration_s = Column(Integer, default=0)
    topic = Column(String(255), default="")
    analyzed = Column(Boolean, default=False, index=True)
    analyzed_at = Column(DateTime(timezone=True), nullable=True)
    creator = relationship("Creator", back_populates="videos")
    sponsorships = relationship("Sponsorship", back_populates="video", cascade="all,delete")
    __table_args__ = (Index("ix_videos_creator_pub", "creator_id", "published_at"),)

class Sponsorship(Base):
    __tablename__ = "sponsorships"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("videos.id"), index=True)
    brand = Column(String(255), index=True)
    brand_norm = Column(String(255), index=True)  # lowercased for search
    category = Column(String(128), index=True, default="unknown")
    confidence = Column(Float, default=0.0)
    evidence = Column(Text, default="")       # exact quote / description line
    link = Column(String(1024), default="")   # brand URL from description
    method = Column(String(32), default="regex")  # regex | groq | both
    detected_at = Column(DateTime(timezone=True), default=now, index=True)
    video = relationship("Video", back_populates="sponsorships")
    __table_args__ = (
        Index("ix_spon_brand_pub", "brand_norm", "detected_at"),
        Index("ix_spon_cat_brand", "category", "brand_norm"),
        UniqueConstraint("video_id", "brand_norm", name="uq_video_brand"),
    )
