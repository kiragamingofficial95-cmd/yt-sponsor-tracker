from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./tracker.db"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
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

    class Config:
        env_file = ".env"

settings = Settings()
