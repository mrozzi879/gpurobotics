# bot.py — Koyeb-friendly Reddit sniper with OCR+image send
import asyncio, aiohttp, re, json, os, sys, time, io
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter
import pytesseract

# ========== CONFIG ==========
SUBREDDITS = ["xboxgamepass", "Xbox", "gamesir", "xboxindia"]
KEYWORDS = ["code", "free", "giveaway", "game pass code", "gamepass"]
POLL_DELAY = float(os.environ.get("POLL_DELAY", "2.5"))  # recommended 2.5-3.0 on Koyeb free
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
USER_AGENT = "linux:sniper.koyeb:v1.0 (by u/your_reddit_username)"
SEEN_FILE = "seen_ids.json"
REQUEST_TIMEOUT = 12
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "4"))
MAX_IMG_PER_POST = 2
# ============================

if not DISCORD_WEBHOOK:
    print("ERROR: Set DISCORD_WEBHOOK environment variable in your deploy settings.")
    sys.exit(1)

# build keyword regex (whole word, case-insensitive)
escaped = [re.escape(k) for k in KEYWORDS]
pattern = r"\b(?:" + "|".join(escaped) + r")\b"
KW_RE = re.compile(pattern, flags=re.IGNORECASE)

# code regexes
CODE_RE_DASH = re.compile(r"\b[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}\b")
CODE_RE_25 = re.compile(r"\b[A-Z0-9]{25}\b")

seen_path = Path(SEEN_FILE)
if seen_path.exists():
    try:
        seen_ids = set(json.loads(seen_path.read_text()))
    except Exception:
        seen_ids = set()
else:
    seen_ids = set()

semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ---------- Reddit fetch ----------
async def fetch_subreddit(session, sub):
    url = f"https://www.reddit.com/r/{sub}/new.json?limit=25"
    headers = {"User-Agent": USER_AGENT}
    async with semaphore:
        try:
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as r:
                if r.status == 429:
                    return {"sub": sub, "error": "rate_limited", "status": 429}
                r.raise_for_status()
                data = await r.json()
                items = []
                for child in data.get("data", {}).get("children", []):
                    d = child.get("data", {})
                    items.append({
                        "id": d.get("id"),
                        "title": d.get("title",""),
                        "selftext": d.get("selftext",""),
                        "permalink": d.get("permalink",""),
                        "url": d.get("url",""),
                        "subreddit": d.get("subreddit"),
                        "preview": d.get("preview"),
                        "is_gallery": d.get("is_gallery"),
                        "media_metadata": d.get("media_metadata")
                    })
                return {"sub": sub, "items": items}
        except Exception as e:
            return {"sub": sub, "error": str(e)}

# ---------- Discord helpers ----------
async def post_discord(session, content):
    payload = {"content": content}
    try:
        async with session.post(DISCORD_WEBHOOK, json=payload, timeout=10) as r:
            return r.status in (200,204)
    except Exception as e:
        print("Discord post exception:", e)
        return False

async def post_discord_image(session, content, image_bytes, filename="image.png"):
    try:
        form = aiohttp.FormData()
        form.add_field("content", content)
        form.add_field("file", image_bytes, filename=filename, content_type="image/png")
        async with session.post(DISCORD_WEBHOOK, data=form, timeout=20) as r:
            return r.status in (200,204)
    except Exception as e:
        print("Discord image post exception:", e)
        return False

# ---------- OCR helpers ----------
def preprocess_image_for_ocr(img_bytes):
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    gray = ImageOps.grayscale(img)
    w,h = gray.size
    if max(w,h) < 1000:
        scale = 2
        if max(w,h) < 600:
            scale = 3
        gray = gray.resize((w*scale, h*scale), Image.BICUBIC)
    gray = gray.filter(ImageFilter.SHARPEN)
    bw = gray.point(lambda x: 0 if x < 140 else 255, '1')
    return bw.convert("L")

def run_tesseract_on_image(pil_image):
    tconfig = "--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    try:
        text = pytesseract.image_to_string(pil_image, config=tconfig)
        return text
    except Exception:
        return ""

def find_codes_in_text(text):
    if not text:
        return []
    txt = text.upper()
    codes = set()
    for m in CODE_RE_DASH.findall(txt):
        codes.add(m)
    for m in CODE_RE_25.findall(txt):
        codes.add(m)
    return list(codes)

# image URL extraction
async def extract_image_urls_from_post(post):
    urls = []
    preview = post.get("preview")
    if preview:
        for img in preview.get("images", []):
            src = img.get("source", {}).get("url") or (img.get("resolutions") or [{}])[-1].get("url")
            if src:
                urls.append(src.replace("&amp;", "&"))
    if post.get("is_gallery"):
        md = post.get("media_metadata") or {}
        for k,v in md.items():
            if isinstance(v, dict) and v.get("status") == "valid":
                s = v.get("s", {}).get("u") or (v.get("p") or [{}])[-1].get("u")
                if s:
                    urls.append(s.replace("&amp;", "&"))
    url = post.get("url","")
    if url and url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        urls.append(url)
    out = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out

async def download_image_bytes(session, url):
    try:
        async with session.get(url, timeout=15) as r:
            r.raise_for_status()
            return await r.read()
    except Exception:
        return None

async def process_images_for_codes(session, post):
    urls = await extract_image_urls_from_post(post)
    if not urls:
        return [], []
    codes_found = []
    images_sent = []
    for url in urls[:MAX_IMG_PER_POST]:
        b = await download_image_bytes(session, url)
        if not b: continue
        pil = preprocess_image_for_ocr(b)
        if pil is None: continue
        text = run_tesseract_on_image(pil)
        codes = find_codes_in_text(text)
        if codes:
            for c in codes:
                codes_found.append(c)
        images_sent.append((b, url.split("/")[-1].split("?")[0] or "image.png"))
    return codes_found, images_sent

def matches_any(post):
    text = (post.get("title","") + " " + post.get("selftext",""))
    return bool(KW_RE.search(text))

async def check_all_once(session):
    tasks = [fetch_subreddit(session, sub) for sub in SUBREDDITS]
    results = await asyncio.gather(*tasks)
    alerted = 0
    for res in results:
        if res.get("error"):
            if res.get("status")==429:
                print("Rate limited by Reddit on", res["sub"])
            else:
                print("Error fetching", res.get("sub"), ":", res.get("error"))
            continue
        for p in res.get("items", []):
            pid = p.get("id")
            if not pid or pid in seen_ids:
                continue
            textmatch = matches_any(p)
            codes_from_img, images = [], []
            if textmatch or p.get("preview") or p.get("is_gallery"):
                codes_from_img, images = await process_images_for_codes(session, p)
            combined_text = (p.get("title","") + " " + p.get("selftext","")).upper()
            text_codes = []
            for m in CODE_RE_DASH.findall(combined_text):
                text_codes.append(m)
            for m in CODE_RE_25.findall(combined_text):
                text_codes.append(m)
            all_codes = list(dict.fromkeys(text_codes + codes_from_img))
            if all_codes:
                link = "https://reddit.com" + p.get("permalink","")
                for code in all_codes:
                    msg = f"🔔 **Match r/{p.get('subreddit')}** — {p.get('title')}\nCode: `{code}`\n{link}"
                    await post_discord(session, msg)
                if images:
                    img_bytes, filename = images[0]
                    ocr_summary = " | ".join(all_codes) if all_codes else "No codes"
                    msg2 = f"Image from r/{p.get('subreddit')} — OCR found: {ocr_summary}\n{link}"
                    await post_discord_image(session, msg2, img_bytes, filename=filename)
                alerted += 1
                seen_ids.add(pid)
            else:
                if textmatch and (p.get("preview") or p.get("is_gallery")):
                    if images:
                        img_bytes, filename = images[0]
                        msg2 = f"⚠️ Keyword match but no code detected by OCR in r/{p.get('subreddit')}: {p.get('title')}\nhttps://reddit.com{p.get('permalink','')}"
                        await post_discord_image(session, msg2, img_bytes, filename=filename)
                        seen_ids.add(pid)
    return alerted

async def main_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                alerted = await check_all_once(session)
                if alerted:
                    print(time.strftime("%Y-%m-%d %H:%M:%S"), "alerts:", alerted)
                try:
                    seen_path.write_text(json.dumps(sorted(list(seen_ids))))
                except Exception as e:
                    print("Failed to save seen ids:", e)
                await asyncio.sleep(POLL_DELAY)
            except Exception as e:
                print("Loop exception:", e)
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main_loop())
