from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./tracker.db"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    # 24/7 worker tuning — meets "1 brand / 2h max" requirement
    SCAN_INTERVAL_SEC: int = 300          # full sweep every 5 min
    VIDEOS_PER_CREATOR: int = 8           # recent videos per creator per sweep
    MAX_WORKERS: int = 10                 # concurrent video analyses
    # endless niche hunt: keep discovering + digging until every creator yields a brand
    MAX_NEW_CREATORS_PER_SWEEP: int = 15  # new creators added per niche per sweep
    HISTORY_CREATORS_PER_SWEEP: int = 5   # creators whose history gets dug per sweep
    HISTORY_VIDEOS_PER_SWEEP: int = 25    # old videos dug per history creator per sweep
    HISTORY_MAX_VIDEOS: int = 300         # give up digging after this many videos
    RECENT_CREATORS_PER_SWEEP: int = 30   # recent-upload checks per sweep (rotates)
    RUN_WORKER: bool = True
    PORT: int = 8000
    # anti-429: optional HTTP(S) proxies, comma-separated (round-robin).
    # e.g. PROXY_URLS="http://user:pass@1.2.3.4:8080,http://user:pass@5.6.7.8:8080"
    PROXY_URLS: str = ""
    YT_MIN_INTERVAL_SEC: float = 1.0  # min gap between yt-dlp network calls
    YT_RETRIES: int = 3             # attempts per call, rotating proxy+client

    class Config:
        env_file = ".env"

settings = Settings()
