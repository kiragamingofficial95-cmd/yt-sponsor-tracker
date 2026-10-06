"""Two-stage brand detection (fast path guarantees >=1 brand/2h):
Stage 1 regex/description-links (no LLM, ~seconds) -> Stage 2 Groq LLM confirm/enrich.
"""
import re, json, os
from typing import List, Dict

HINTS = re.compile(
    r"(this video (is )?sponsor(ed)? by|sponsored by|thanks? to (our )?sponsor|"
    r"partnered with|use (my )?code|discount code|link in (the )?description|"
    r"paid promotion|brand deal|gifted by|#ad\b|\bAD\b|affiliate link)",
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

def stage1_candidates(description: str, pinned: str, transcript: str) -> Dict:
    text = f"{description}\n{pinned}\n{transcript[:4000]}"
    score = len(HINTS.findall(text))
    found_brands = [b for b in BRANDS if re.search(r"\b" + re.escape(b) + r"\b", text, re.I)]
    links = []
    for m in URL_RE.finditer(description + "\n" + pinned):
        dom = m.group(1).lower()
        if dom not in NOISE_DOMAINS and len(dom) > 2:
            links.append(m.group(0))
    links = list(dict.fromkeys(links))[:10]
    return {"score": score, "brands": found_brands[:8], "links": links,
            "suspicious": score > 0 or bool(found_brands) or len(links) > 0}

GROQ_PROMPT = """Analyze this YouTube video text (description + pinned comment + transcript excerpt) and extract SPONSORSHIPS.
Return ONLY valid JSON: {"sponsorships":[{"brand":"...","category":"...","confidence":0.0-1.0,"evidence":"short exact quote"}]}
Categories: VPN,Hosting,Education,Food,Finance,Fashion,Tech,Gaming,Health,Beauty,Travel,Other.
If no sponsorship, return {"sponsorships":[]}.
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
            if s.get("brand"):
                out.append({"brand": s["brand"].strip()[:120],
                            "category": s.get("category", "Other")[:64],
                            "confidence": float(s.get("confidence", 0.8)),
                            "evidence": s.get("evidence", "")[:600]})
        return out
    except Exception:
        return []

def detect(description: str, pinned: str, transcript: str,
           api_key: str = "", model: str = "openai/gpt-oss-120b") -> List[Dict]:
    """Returns list of {brand, category, confidence, evidence, link, method}."""
    c = stage1_candidates(description, pinned, transcript)
    results: List[Dict] = []
    link0 = c["links"][0] if c["links"] else ""
    # fast regex path — always emits when a known brand is named near a hint
    for b in c["brands"]:
        results.append({"brand": b, "category": "Other", "confidence": 0.65 if c["score"] else 0.45,
                        "evidence": (pinned or description)[:300], "link": link0, "method": "regex"})
    # Groq confirm/enrich — suspicious videos OR long texts (unlisted brands hide there)
    text_len = len(description) + len(pinned) + len(transcript)
    if api_key and (c["suspicious"] or text_len > 1500):
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
    return results
