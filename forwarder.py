import os
import re
import io
import asyncio
import hashlib
import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl

logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SOURCES = ["deals_me", "OffersEgyptofficial", "Raw5aa"]
TARGET = "PromoCodeOnlain"
FOOTER = "\n\nقناتنا: @PromoCodeOnlain"
MY_TAG = "mixdeals-21"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}
URL_RE = re.compile(r"https?://[^\s]+")
AMAZON_RE = re.compile(r"amazon\.|amzn\.|link\.amazon")

seen = set()


def clean(text):
    lines = []
    for line in text.splitlines():
        if "t.me/" in line or re.search(r"@\w+", line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


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
        bio = io.BytesIO(data)
        bio.name = "product.jpg"
        return bio
    except Exception as e:
        logging.warning("Image fetch failed: %s", e)
        return None


async def main():
    client = TelegramClient("session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=SOURCES))
    async def handler(event):
        msg = event.message
        raw = msg.message or ""

        urls = URL_RE.findall(raw)
        for ent in msg.entities or []:
            if isinstance(ent, MessageEntityTextUrl):
                urls.append(ent.url)
        urls = [u.rstrip(").,") for u in urls if "t.me" not in u]

        text = clean(raw)
        if not text:
            return

        new_urls = []
        for u in urls:
            new_urls.append(await asyncio.to_thread(to_my_link, u))
        for old, new in zip(urls, new_urls):
            text = text.replace(old, new)
        if new_urls and new_urls[0] not in text:
            text += "\n\n" + new_urls[0]

        key = hashlib.md5(text.encode()).hexdigest()
        if key in seen:
            return
        seen.add(key)

        full = text + FOOTER
        image = None
        if new_urls:
            image = await asyncio.to_thread(get_image, new_urls[0])

        try:
            if image and len(full) <= 1000:
                await client.send_message(TARGET, full, file=image)
            elif image:
                await client.send_message(TARGET, "", file=image)
                await client.send_message(TARGET, full, link_preview=False)
            else:
                await client.send_message(TARGET, full, link_preview=False)
            logging.info("Posted: %s", text[:40])
        except Exception as e:
            logging.warning("Failed to post: %s", e)
        await asyncio.sleep(3)

    await client.start()
    logging.info("Running. Waiting for new posts...")
    await client.run_until_disconnected()


asyncio.run(main())
