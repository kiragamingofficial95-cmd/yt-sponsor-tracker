"""Two-stage brand detection (fast path guarantees >=1 brand/2h):
Stage 1 regex/description-links (no LLM, ~seconds) -> Stage 2 Groq LLM confirm/enrich.
"""
import re, json, os
from typing import List, Dict

HINTS = re.compile(
    r"(this video (is )?sponsor(ed)? by|sponsored by|sponsor segment|"
    r"sponsored segment|thanks? to (our )?sponsor|"
    r"partnered with|paid partnership|paid promotion|brand deal|gifted by|"
    r"use (my |our )?code|discount code|promo code|coupon code|"
    r"get \d+\s?% off|save \d+\s?%|free trial|try .* free|"
    r"go to .*\.com/|link in (the )?description|check out .* in the description|"
    r"affiliate link|#ad\b|\bAD\b)",
    re.I,
)
# Strong promo signals — brand must be NEAR one of these to count offline.
PROMO_RE = re.compile(
    r"(sponsor|partnered|paid promotion|brand deal|gifted|#ad\b|\bAD\b|"
    r"affiliate|discount|promo|coupon|code |% off|free trial|"
    r"link in (the )?description|go to |try |download |sign up)",
    re.I,
)
# Contexts that are NEVER sponsorships even if a known brand is named.
EXCLUDE_RE = re.compile(
    r"(music (from|by|provided|courtesy)|songs? (from|by)|"
    r"footage (from|by|provided)|video (from|by)|"
    r"copyright|all rights reserved|fair use|"
    r"shot on|filmed on|edited (with|on|in)|"
    r"follow me on|subscribe|merch)",
    re.I,
)
URL_RE = re.compile(r"https?://(?:www\.)?([a-z0-9-]+)\.([a-z]{2,})", re.I)
NOISE_DOMAINS = {"youtube", "youtu", "google", "facebook", "instagram", "twitter",
                 "tiktok", "discord", "patreon", "paypal", "amzn", "amazon", "bit"}

def load_brand_list() -> List[str]:
    p = os.path.join(os.path.dirname(__file__), "..", "data", "brands.txt")
    try:
        with open(p) as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return ["NordVPN", "Skillshare", "Squarespace", "LinkedIn Learning", "Brilliant",
                "HelloFresh", "Ridge", "Audible", "ExpressVPN", "BetterHelp"]

BRANDS = load_brand_list()

def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?\n])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _brand_windows(text: str, brand: str, radius: int = 80) -> List[str]:
    # sentence-bound: promo must be in the SAME sentence as the brand,
    # so Ridge's "20% off" can't leak onto Apple mentioned 2 sentences later.
    sents = _sentences(text)
    out = []
    b_pat = re.compile(r"\b" + re.escape(brand) + r"\b", re.I)
    for i, s in enumerate(sents):
        if b_pat.search(s):
            out.append(s)
            if len(out) >= 4:
                break
    if out:
        return out
    # fallback to char windows (very long sentences / transcripts without punctuation)
    out = []
    for m in re.finditer(r"\b" + re.escape(brand) + r"\b", text, re.I):
        s = max(0, m.start() - radius)
        out.append(text[s:m.end() + radius])
    return out[:4]


def _is_sponsored_window(brand: str, window: str, link_blob_norm: str) -> bool:
    """Brand must be BOUND to promo — not just share a paragraph with it."""
    b_esc = re.escape(brand)
    # music / footage credit veto unless brand itself has code/sponsor binding
    if re.search(r"music (from|by|provided|courtesy)|songs? (from|by)|"
                 r"footage (from|by|provided)", window, re.I):
        if not re.search(
            r"(sponsored by|partnered|paid promotion|brand deal|gifted by|#ad\b|\bAD\b|"
            r"affiliate|discount|promo|coupon|code |% off|free trial).{0,40}" + b_esc +
            r"|" + b_esc + r".{0,40}(code|% off|free trial|discount|promo|coupon|#ad\b|sponsored)",
                window, re.I):
            return False
    # 1. explicit sponsor phrase bound to this brand
    if re.search(
        r"(sponsor(ed)? by|sponsor segment|sponsored segment|thanks to|"
        r"partnered with|paid partnership|paid promotion|brand deal|gifted by)"
        r".{0,60}" + b_esc + r"|" + b_esc +
        r".{0,60}(sponsor|partnered|paid promotion|brand deal|gifted)",
            window, re.I):
        return True
    # 2. promo code / discount / trial bound to this brand
    if re.search(
        b_esc + r".{0,60}(use (my |our )?code|discount code|promo code|coupon code|"
        r"code |% off|free trial|link in (the )?description|go to |try .* free|"
        r"affiliate|#ad\b)",
        window, re.I) or re.search(
        r"(use (my |our )?code|discount|promo|coupon|% off|free trial|"
        r"sponsored by|partnered).{0,60}" + b_esc, window, re.I):
        return True
    # 3. brand's own tracked link inside the same window
    bkey = re.sub(r"[^a-z0-9]", "", brand.lower())
    if len(bkey) > 3 and bkey in link_blob_norm:
        for m in URL_RE.finditer(window):
            dom = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
            if dom and (dom in bkey or bkey in dom):
                return True
    return False


def stage1_candidates(description: str, pinned: str, transcript: str) -> Dict:
    text = f"{description}\n{pinned}\n{transcript[:6000]}"
    score = len(HINTS.findall(text))
    links = []
    for m in URL_RE.finditer(description + "\n" + pinned):
        dom = m.group(1).lower()
        if dom not in NOISE_DOMAINS and len(dom) > 2:
            links.append(m.group(0))
    links = list(dict.fromkeys(links))[:10]
    link_blob = " ".join(links).lower()

    confirmed, mentions = [], []
    for b in BRANDS:
        if not re.search(r"\b" + re.escape(b) + r"\b", text, re.I):
            continue
        wins = _brand_windows(text, b)
        strong = any(_is_sponsored_window(b, w, re.sub(r"[^a-z0-9]", "", link_blob)) for w in wins)
        (confirmed if strong else mentions).append(b)
        if len(confirmed) >= 8:
            break
    return {"score": score, "brands": confirmed[:8], "mentions": mentions[:12],
            "links": links,
            "suspicious": score > 0 and bool(confirmed)}

GROQ_PROMPT = """You extract PAID SPONSORSHIPS from YouTube video text (description + pinned comment + transcript excerpt).
A SPONSORSHIP needs explicit promo proof: "sponsored by X", "thanks to X for sponsoring", "use code X", "X% off", "free trial at X.com/link", "link in description for X", "#ad", "paid promotion", "partnered with X", "gifted by X" + promo, or a dedicated sponsor segment with timestamps.

NOT a sponsorship (return nothing for these):
- Product REVIEWED, compared, benchmarked, or critiqued (e.g. iPhone vs Xiaomi, "Apple iPhone Duo camera is great").
- Brand merely TALKED ABOUT, shown in B-roll, or named as subject.
- Music / SFX library credit: "Music from Epidemic Sound / Artlist" — that is a library, not a sponsor, unless it says "sponsored by Epidemic Sound" + code/link.
- Stock footage, editing software credit, "Shot on Sony", "Edited in Premiere" without promo.
- Social links, copyright / fair-use notices.
- Past sponsors mentioned without a current promo.

Return ONLY valid JSON: {"sponsorships":[{"brand":"...","category":"...","confidence":0.0-1.0,"evidence":"short exact quote showing PROMO"}]}
Categories: VPN,Hosting,Education,Food,Finance,Fashion,Tech,Gaming,Health,Beauty,Travel,Other.
Evidence MUST be a real promo quote (e.g. "thanks to Surfshark for sponsoring, get 4 months free"). If only discussed/credited, return {"sponsorships":[]}.
Confidence <0.6 = reject. Be strict: when in doubt, return [].
TEXT:
"""

def stage2_groq(description: str, pinned: str, transcript: str, api_key: str, model: str) -> List[Dict]:
    if not api_key:
        return []
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        blob = f"DESCRIPTION:\n{description[:3500]}\nPINNED:\n{pinned[:1500]}\nTRANSCRIPT:\n{transcript[:5000]}"
        r = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "user", "content": GROQ_PROMPT + blob}],
            max_tokens=800)
        txt = r.choices[0].message.content or "{}"
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = []
        for s in data.get("sponsorships", [])[:5]:
            if not s.get("brand"):
                continue
            try:
                conf = float(s.get("confidence", 0.8))
            except Exception:
                conf = 0.0
            if conf < 0.6:  # strict: drop guesses / mentions
                continue
            ev = (s.get("evidence") or "")[:600]
            if not PROMO_RE.search(ev) and not PROMO_RE.search(blob[:2000]):
                # LLM must quote promo proof; otherwise it's a mention
                continue
            out.append({"brand": s["brand"].strip()[:120],
                        "category": s.get("category", "Other")[:64],
                        "confidence": conf, "evidence": ev})
        return out
    except Exception:
        return []

def _evidence_window(description: str, pinned: str, transcript: str, brand: str) -> str:
    text = f"{description}\n{pinned}\n{transcript[:6000]}"
    wins = _brand_windows(text, brand)
    for w in wins:
        if PROMO_RE.search(w):
            return w.strip()[:600]
    return (wins[0].strip()[:600] if wins else (pinned or description)[:300])

def detect(description: str, pinned: str, transcript: str,
           api_key: str = "", model: str = "openai/gpt-oss-120b") -> List[Dict]:
    """Returns list of {brand, category, confidence, evidence, link, method}."""
    c = stage1_candidates(description, pinned, transcript)
    results: List[Dict] = []
    link0 = c["links"][0] if c["links"] else ""
    # offline regex path — ONLY when brand is near promo proof, never for mentions
    for b in c["brands"]:
        results.append({"brand": b, "category": "Other", "confidence": 0.75,
                        "evidence": _evidence_window(description, pinned, transcript, b),
                        "link": link0, "method": "regex"})
    # Groq is authoritative when available: confirm + find unlisted, reject mentions
    text_len = len(description) + len(pinned) + len(transcript)
    groq_ran = False
    if api_key and (c["suspicious"] or c["brands"] or text_len > 1500):
        groq_ran = True
        for g in stage2_groq(description, pinned, transcript, api_key, model):
            g["link"] = link0
            g["method"] = "groq"
            if g["brand"].lower() not in {r["brand"].lower() for r in results}:
                results.append(g)
            else:  # upgrade regex hit with LLM category/confidence
                for r in results:
                    if r["brand"].lower() == g["brand"].lower():
                        r.update(category=g["category"], confidence=max(r["confidence"], g["confidence"]),
                                 evidence=g.get("evidence") or r["evidence"], method="both")
        if groq_ran:
            # drop regex-only hits Groq rejected (mentions like Apple / Epidemic Sound)
            gnames = set()
            # re-fetch: stage2 already filtered, so keep only confirmed names
            # if Groq returned [] but regex fired, keep only link-backed hits
            if not any(r["method"] in ("groq", "both") for r in results):
                results = [r for r in results
                           if link0 and r["brand"].lower().replace(" ", "") in
                           re.sub(r"[^a-z0-9]", "", link0.lower())]
    return results
