# YT Sponsorship Analyzer 24/7

Quota-free YouTube sponsorship tracker: scrapes creators/videos (RSS + yt-dlp, **no YouTube API key needed**),
scans **description + pinned comment + transcript**, detects brands via regex fast-path + **Groq LLM**,
stores everything in Postgres, and serves a searchable dashboard.

## Speed guarantee (≥1 brand / 2h)
- `SCAN_INTERVAL_SEC=300`: full sweep every 5 min
- `VIDEOS_PER_CREATOR=8`, `MAX_WORKERS=10` concurrent analyses
- Two-stage detection: regex fast-path emits instantly (~seconds), Groq only runs on suspicious videos (saves quota)
- Even with zero Groq quota, regex + brand list (~400 brands) keeps finding deals

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env   # put GROQ_API_KEY in .env
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

## How to use (dashboard at `/`)
1. Type a niche like `tech SaaS` or `healthcare` → auto-discovers ~10 creators via scraping
2. Or add a creator: channel URL, `UC...` id, or `@handle`
3. Hit **⚡ Scan now** or wait for the 24/7 loop (every 5 min)
4. Search brands, open **Brand deep-dive**: per-creator counts, per-topic, per-year timeline

## Deploy (Railway) — already done for this repo
- Service: Docker (Dockerfile), healthcheck `/health`, restart ALWAYS
- Postgres plugin added; app reads `DATABASE_URL` (use reference `${{Postgres.DATABASE_URL}}`)
- Env vars: `GROQ_API_KEY`, `GROQ_MODEL=llama-3.3-70b-versatile`, `SCAN_INTERVAL_SEC=300`,
  `VIDEOS_PER_CREATOR=8`, `MAX_WORKERS=10`, `RUN_WORKER=True`
- Generate a public domain: Railway dashboard → service → Settings → Networking → Generate Domain

## API
- `GET /api/search?q=nordvpn&category=VPN&year_from=2022&year_to=2026`
- `GET /api/brand/{name}` → total, per_creator, per_topic, per_year, videos
- `POST /api/niche?niche=tech SaaS&limit=10`, `POST /api/creators?channel=@mkbhd`
- `POST /api/scan`, `GET /api/stats`, `GET /health`
