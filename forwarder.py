import os
import re
import io
import time
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
FOOTER = (
    "📢 قناة عروض وخصومات\n"
    "للمزيد من العروض يوميًا:\n"
    "https://t.me/PromoCodeOnlain"
)

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
        return out.ge
