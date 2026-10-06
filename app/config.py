from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./tracker.db"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    # 24/7 worker tuning — meets "1 brand / 2h max" requirement
    SCAN_INTERVAL_SEC: int = 300          # full sweep every 5 min
    VIDEOS_PER_CREATOR: int = 8           # recent videos per creator per sweep
    MAX_WORKERS: int = 10                 # concurrent video analyses
    RUN_WORKER: bool = True
    PORT: int = 8000

    class Config:
        env_file = ".env"

settings = Settings()
