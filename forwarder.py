import os
import re
import io
import html
import asyncio
import hashlib
import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl

logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SOURCES = ["arkhashomoffers", "OffersEgyptofficial", "Raw5aa"]
PRIVATE_TITLE = "مسودة العروض"
MY_TAG = "mixdeals-21"
FOOTER = "📢 قناة عروض وخصومات\nللمزيد من العروض يوميًا: @PromoCodeOnlain"

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
LOGO_RATIO = 0.22  # حجم الشعار نسبة لعرض الصورة

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}
URL_RE = re.compile(r"https?://[^\s]+")
AMAZON_RE = re.compile(r"amazon\.|amzn\.|link\.amazon")

PROMO_WORDS = [
    "واتساب", "واتس اب", "whatsapp", "تابعنا", "تابعونا", "للانضمام",
    "انضم", "اشترك", "جروب", "قناتنا", "قنواتنا", "منصات التواصل",
    "فيسبوك", "facebook", "انستجرام", "انستغرام", "instagram",
    "تيك توك", "tiktok", "تليجرام", "telegram",
]
BAD_URL_PARTS = [
    "t.me", "wa.me", "whatsapp.com", "facebook.com", "fb.com",
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be",
]

seen = set()
target_box = {}


def clean_lines(raw):
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            lines.append("")
            continue
        low = s.lower()
        if any(w in low for w in PROMO_WORDS):
            continue
        if any(b in low for b in BAD_URL_PARTS) or re.search(r"@\w+", s):
            continue
        if re.fullmatch(r"[\W_]+", s):
            continue
        lines.append(s)
    return lines


def to_my_link(url):
    """يحوّل لينك أمازون للينك بكودك. أي لينك تاني بيرجع زي ما هو."""
    if not AMAZON_RE.search(url):
        return url
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        final = r.url
        host = urlparse(final).netloc or "www.amazon.eg"
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", final)
        if m:
            return f"https://{host}/dp/{m.group(1)}?tag={MY_TAG}"
        if "tag=" in final:
            return re.sub(r"([?&])tag=[^&]*", r"\1tag=" + MY_TAG, final)
        sep = "&" if "?" in final else "?"
        return f"{final}{sep}tag={MY_TAG}"
    except Exception as e:
        logging.warning("Link resolve failed: %s", e)
        return url


def add_logo(data):
    """يحط الشعار في الركن التحتاني الأيمن. لو حصل أي خطأ بيرجع الصورة زي ما هي."""
    try:
        base = Image.open(io.BytesIO(data)).convert("RGBA")
        logo = Image.open(LOGO_PATH).convert("RGBA")
        w = max(int(base.width * LOGO_RATIO), 60)
        h = int(logo.height * w / logo.width)
        logo = logo.resize((w, h), Image.LANCZOS)
        margin = int(base.width * 0.03)
        base.alpha_composite(logo, (base.width - w - margin, base.height - h - margin))
        out = io.BytesIO()
        base.convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        logging.warning("Logo overlay failed: %s", e)
        return data


def get_image(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        img_url = None
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            img_url = tag["content"]
        if not img_url:
            img = soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
            if img:
                img_url = img.get("data-old-hires") or img.get("src")
        if not img_url or not img_url.startswith("http"):
            return None
        data = requests.get(img_url, headers=HEADERS, timeout=20).content
        if len(data) < 2000:
            return None
        data = add_logo(data)
        bio = io.BytesIO(data)
        bio.name = "product.jpg"
        return bio
    except Exception as e:
        logging.warning("Image fetch failed: %s", e)
        return None


def replace_all(line, olds, news):
    for o, n in zip(olds, news):
        line = line.replace(o, n)
    return line


def build_post(lines, urls):
    esc = html.escape
    single = len(set(urls)) <= 1

    if single:
        lines = [l for l in lines if "http" not in l]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return None

    parts = ["🔥 <b>" + esc(lines[0]) + "</b>"]
    rest = "\n".join(lines[1:]).strip()
    rest = re.sub(r"\n{3,}", "\n\n", rest)
    if rest:
        parts.append(esc(rest))
    if single and urls:
        parts.append('🛒 <a href="' + esc(urls[0], quote=True) + '">اشتري من هنا</a>')
    parts.append(esc(FOOTER))
    return "\n\n".join(parts)


async def main():
    client = TelegramClient("session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=SOURCES))
    async def handler(event):
        target = target_box.get("entity")
        if target is None:
            return

        msg = event.message
        raw = msg.message or ""

        urls = URL_RE.findall(raw)
        for ent in msg.entities or []:
            if isinstance(ent, MessageEntityTextUrl):
                urls.append(ent.url)
        urls = [u.rstrip(").,") for u in urls]
        urls = [u for u in urls if not any(b in u.lower() for b in BAD_URL_PARTS)]

        lines = clean_lines(raw)
        if not any(lines):
            return

        new_urls = []
        for u in urls:
            new_urls.append(await asyncio.to_thread(to_my_link, u))
        lines = [replace_all(l, urls, new_urls) for l in lines]

        uniq = list(dict.fromkeys(new_urls))
        post = build_post(lines, uniq)
        if not post:
            return

        key = hashlib.md5(post.encode()).hexdigest()
        if key in seen:
            return
        seen.add(key)

        image = None
        if uniq:
            image = await asyncio.to_thread(get_image, uniq[0])

        try:
            if image and len(post) <= 1000:
                await client.send_message(target, post, file=image, parse_mode="html")
            elif image:
                await client.send_message(target, "", file=image)
                await client.send_message(target, post, parse_mode="html", link_preview=False)
            else:
                await client.send_message(target, post, parse_mode="html", link_preview=False)
            logging.info("Posted: %s", lines[0][:40])
        except Exception as e:
            logging.warning("Failed to post: %s", e)
        await asyncio.sleep(3)

    await client.start()

    names = []
    async for d in client.iter_dialogs():
        names.append(d.name)
        if d.name == PRIVATE_TITLE:
            target_box["entity"] = d.entity
            break
    if "entity" not in target_box:
        logging.error("Channel '%s' not found. Your chats: %s", PRIVATE_TITLE, names[:15])
        return

    logging.info("Running. Posting to private channel: %s", PRIVATE_TITLE)
    await client.run_until_disconnected()


asyncio.run(main())
