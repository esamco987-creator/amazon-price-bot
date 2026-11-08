import os
import re
import sys
import json
import time
import html
import asyncio
import hashlib
import logging
from urllib.parse import urlparse

import requests
from telethon import TelegramClient, events, utils
from telethon.tl.types import MessageEntityTextUrl

import post_bot as pb
import draft_bot as db
import deals_scanner as scanner

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

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
        final, body = pb.resolve(url, pb.HEADER_STRATEGIES[0])
        host = urlparse(final).netloc
        if "amazon." not in host or host.startswith("link."):
            host = "www.amazon.eg"
        asin = pb.find_asin(final) or pb.find_asin(url) or pb.find_asin(body)
        if asin:
            return f"https://{host}/dp/{asin}?tag={MY_TAG}"
        if "tag=" in final:
            return re.sub(r"([?&])tag=[^&]*", r"\1tag=" + MY_TAG, final)
        sep = "&" if "?" in final else "?"
        return f"{final}{sep}tag={MY_TAG}"
    except Exception as e:
        logging.warning("Link resolve failed: %s", type(e).__name__)
        return url


def get_image(url):
    """صورة المنتج بنفس شكل post_bot: خلفية بيضاء ومنتج في النص والشعار في الركن."""
    try:
        if "amazon." not in urlparse(url).netloc:
            return None
        _, page = pb.get_page(url, [])
        if not page:
            return None
        img = pb.read_product(page)[4]
        raw = pb.download_image(img)
        return pb.prepare_image(raw) if raw else None
    except Exception as e:
        logging.warning("Image fetch failed: %s", type(e).__name__)
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
        parts.append("🛒 اشتري من هنا:\n" + esc(urls[0]))
    parts.append(esc(FOOTER))
    return "\n\n".join(parts)


def api(method, data, files=None):
    """يرجع message_id لو نجح، أو None."""
    r = requests.post(
        f"https://api.telegram.org/bot{db.TOKEN}/{method}",
        data=data, files=files, timeout=60,
    )
    if r.status_code != 200:
        logging.warning("Bot API %s failed: %s", method, r.text[:200])
        return None
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return True


def send_draft(pid, image, post):
    """يبعت المسودة لقناة المسودات مع أزرار نشر / تعديل / إلغاء. يرجع قايمة الرسايل."""
    chat = target_box["chat_id"]
    kb = json.dumps({
        "inline_keyboard": [
            [
                {"text": "✅ نشر", "callback_data": f"pub:{pid}"},
                {"text": "✏️ تعديل", "callback_data": f"ed:{pid}"},
            ],
            [{"text": "❌ إلغاء", "callback_data": f"cx:{pid}"}],
        ]
    })
    base = {"chat_id": chat, "parse_mode": "HTML"}
    sent = []
    if image and len(post) <= 1000:
        mid = api("sendPhoto", {**base, "caption": post, "reply_markup": kb},
                  {"photo": ("product.jpg", image)})
        return [[chat, mid]] if mid else None
    if image:
        m1 = api("sendPhoto", {"chat_id": chat}, {"photo": ("product.jpg", image)})
        if m1:
            sent.append([chat, m1])
    m2 = api("sendMessage", {**base, "text": post, "reply_markup": kb,
                             "disable_web_page_preview": "true"})
    if not m2:
        return None
    sent.append([chat, m2])
    return sent


async def scan_loop():
    """يدوّر على خصومات كبيرة كل فترة ويبعتها كمسودات."""
    await asyncio.sleep(20)
    while True:
        try:
            results = await asyncio.to_thread(scanner.scan_once)
            for item, post, image in results:
                pid = db.new_pid()
                db.store_draft(pid, image, post)
                sent = await asyncio.to_thread(send_draft, pid, image, post)
                if sent:
                    db.set_src(pid, sent)
                else:
                    db.drop_draft(pid)
                await asyncio.sleep(3)
        except Exception as e:
            logging.warning("scan loop error: %s", type(e).__name__)
        await asyncio.sleep(scanner.SCAN_EVERY)


async def main():
    client = TelegramClient(
        "session", API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
    )

    @client.on(events.NewMessage(chats=SOURCES))
    async def handler(event):
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

        pid = db.new_pid()
        try:
            db.store_draft(pid, image, post)
            sent = await asyncio.to_thread(send_draft, pid, image, post)
            if sent:
                db.set_src(pid, sent)
                logging.info("Draft sent: %s", lines[0][:40])
            else:
                db.drop_draft(pid)
        except Exception as e:
            logging.warning("Failed to send draft: %s", type(e).__name__)
        await asyncio.sleep(3)

    await client.start()

    names = []
    async for d in client.iter_dialogs():
        names.append(d.name)
        if d.name == PRIVATE_TITLE:
            target_box["chat_id"] = utils.get_peer_id(d.entity)
            break
    if "chat_id" not in target_box:
        logging.error("Channel '%s' not found. Your chats: %s", PRIVATE_TITLE, names[:15])
        return

    logging.info("Running. Drafts go to: %s (with buttons)", PRIVATE_TITLE)
    if scanner.enabled():
        target_box["scan_task"] = asyncio.create_task(scan_loop())
        logging.info("Deal scanner on: discount >= %s%% every %s min", scanner.MIN_DISCOUNT, scanner.SCAN_EVERY // 60)
    else:
        logging.info("Deal scanner off (CREATORS_ID / CREATORS_SECRET not set)")
    await client.run_until_disconnected()


if not db.TOKEN:
    print("لازم تعمل export لـ DRAFT_BOT_TOKEN الأول.")
    sys.exit(1)

while True:
    try:
        asyncio.run(main())
        break
    except KeyboardInterrupt:
        break
    except Exception as e:
        logging.warning("Stopped: %s. Restarting in 30 seconds...", type(e).__name__)
        time.sleep(30)
