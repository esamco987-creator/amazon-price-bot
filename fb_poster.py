"""بوت النشر على فيسبوك: بيراقب قناة التليجرام وينشر كل بوست جديد على صفحة إيجي فون.

- بيستخدم نسخة من جلسة Telethon (fbpost.session) عشان ما يتعارضش مع بوت التحويل.
- بيحط البوستات في طابور وينشر بوست كل FB_GAP_MIN دقيقة (افتراضي 15) عشان فيسبوك ما يعتبرهاش سبام.
- بيتجاهل البوست لو نفس المنتج اتنشر في آخر 24 ساعة (يعني الإعادات ما بتتنشرش تاني).
- ساعات الهدوء (افتراضي 2-7 الصبح بتوقيت القاهرة): مفيش نشر.
المطلوب في env.sh: API_ID, API_HASH, FB_PAGE_ID, FB_PAGE_TOKEN
اختياري: FB_GAP_MIN, FB_QUIET (مثلا 1-8 أو off), FB_CHANNEL
"""
import os, re, io, time, sqlite3, asyncio, logging, datetime

import requests
from telethon import TelegramClient, events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("telethon",):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("fb_poster")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "bot_data.db")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PAGE_ID = os.environ["FB_PAGE_ID"]
PAGE_TOKEN = os.environ["FB_PAGE_TOKEN"]
CHANNEL = os.environ.get("FB_CHANNEL", "PromoCodeOnlain")
GAP = float(os.environ.get("FB_GAP_MIN", "15")) * 60
QUIET = os.environ.get("FB_QUIET", "2-7")
GRAPH = "https://graph.facebook.com/v21.0"
MAX_AGE = float(os.environ.get("FB_MAX_AGE_H", "4")) * 3600
ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
HOURS24 = 24 * 3600
TG_LINK = os.environ.get("FB_TG_LINK", "https://t.me/PromoCodeOnlain")
CTAS = [
    "🔥 عروض زي دي كل يوم على قناتنا في تليجرام 👇",
    "🚨 انضم لقناتنا وماتفوتش أي خصم 👇",
    "💸 وفّر فلوسك! عروضنا على تليجرام 👇",
    "🎁 أقوى العروض أول بأول على تليجرام 👇",
    "⚡ عروض جديدة يومياً على قناتنا 👇",
    "👀 في أحلى من كده على قناتنا في تليجرام 👇",
    "🛒 قبل ما تشتري، شوف قناتنا في تليجرام 👇",
    "💥 الخصومات النار بتنزل الأول على تليجرام 👇",
    "🔔 فعّل إشعارات قناتنا وماتفوتش عرض 👇",
    "🏷️ خصومات حقيقية يومياً على تليجرام 👇",
    "🤑 أكواد وعروض يومية على قناتنا 👇",
    "🚀 انضم لقناتنا وخلّي العروض توصلك فوراً 👇",
]
_last_cta = [None]


def with_cta(text):
    pool = [c for c in CTAS if c != _last_cta[0]] or CTAS
    _last_cta[0] = pool[int(time.time()) % len(pool)]
    return f"{text}\n\n{_last_cta[0]}\n{TG_LINK}"


def con():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS fb_queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE, asin TEXT, text TEXT,
        img BLOB, created INTEGER, status TEXT DEFAULT 'pending', posted_ts INTEGER, fb_id TEXT, note TEXT)""")
    return c


def quiet_now():
    if QUIET.lower() in ("", "off", "no", "0"):
        return False
    try:
        a, b = [int(x) for x in QUIET.split("-")]
    except Exception:
        return False
    cairo = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    h = cairo.hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def clean(text):
    t = (text or "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t[:2000]


PRICE_RE = re.compile(r"\d[\d,\.]*\s*(?:جنيه|ج\.م|EGP)|(?:جنيه|EGP)\s*\d", re.I)
REQUIRE_PHOTO = os.environ.get("FB_REQUIRE_PHOTO", "1") != "0"


def incomplete_reason(text, img):
    """بوست كامل = صورة + سعر + لينك. بيرجع سبب النقص أو None."""
    if REQUIRE_PHOTO and not img:
        return "من غير صورة"
    if not PRICE_RE.search(text):
        return "من غير سعر"
    if "http" not in text:
        return "من غير لينك"
    return None


def last_post_ts(db):
    r = db.execute("SELECT MAX(posted_ts) FROM fb_queue WHERE status='posted'").fetchone()
    return r[0] or 0


def post_to_facebook(text, img):
    if img:
        r = requests.post(f"{GRAPH}/{PAGE_ID}/photos", data={"caption": text, "access_token": PAGE_TOKEN},
                          files={"source": ("deal.jpg", img, "image/jpeg")}, timeout=90)
    else:
        r = requests.post(f"{GRAPH}/{PAGE_ID}/feed", data={"message": text, "access_token": PAGE_TOKEN}, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"error": {"message": r.text[:200]}}
    if r.status_code != 200 or "error" in j:
        raise RuntimeError(str(j.get("error", j))[:300])
    return j.get("post_id") or j.get("id")


async def worker():
    db = con()
    while True:
        await asyncio.sleep(30)
        try:
            if quiet_now() or time.time() - last_post_ts(db) < GAP:
                continue
            db.execute("UPDATE fb_queue SET status='expired' WHERE status='pending' AND created<?",
                       (int(time.time() - MAX_AGE),))
            db.commit()
            row = db.execute("SELECT id,text,img FROM fb_queue WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                continue
            qid, text, img = row
            log.info("posting to facebook now (queue id %s, photo=%s)...", qid, bool(img))
            try:
                fid = await asyncio.to_thread(post_to_facebook, with_cta(text), img)
                db.execute("UPDATE fb_queue SET status='posted', posted_ts=?, fb_id=? WHERE id=?",
                           (int(time.time()), fid, qid))
                log.info("posted to facebook: %s", fid)
            except Exception as e:
                msg = str(e)
                log.warning("facebook post failed: %s", msg)
                bad = any(k in msg for k in ("190", "OAuthException", "expired", "permission", "(#200)", "(#10)"))
                db.execute("UPDATE fb_queue SET status=?, note=? WHERE id=?",
                           ("failed", msg[:200], qid))
                if bad:
                    log.error("المفتاح أو الصلاحيات فيها مشكلة. اوقف البوت وراجع الإعداد.")
                    await asyncio.sleep(600)
            db.commit()
        except Exception as e:
            log.warning("worker error: %s", e)


async def main():
    client = TelegramClient(os.path.join(BASE, "fbpost"), API_ID, API_HASH)
    await client.start()
    db = con()
    ent = await client.get_entity(CHANNEL)
    log.info("fb poster on: channel=%s gap=%smin quiet=%s", CHANNEL, GAP / 60, QUIET)

    last_photo = {"img": None, "ts": 0}

    @client.on(events.NewMessage(chats=ent))
    async def on_msg(ev):
        m = ev.message
        text = clean(m.message)
        img = None
        if m.photo:
            try:
                buf = io.BytesIO()
                await client.download_media(m, file=buf)
                img = buf.getvalue()
            except Exception as e:
                log.warning("photo download failed: %s", e)
        if not text:
            if img:                       # صورة لوحدها: البوت بيبعت النص في رسالة بعدها
                last_photo.update(img=img, ts=time.time())
            return
        if not img and last_photo["img"] and time.time() - last_photo["ts"] < 120:
            img = last_photo["img"]       # النص جه بعد صورته مباشرة
        last_photo["img"] = None
        why = incomplete_reason(text, img)
        if why:
            log.info("skip msg %s (بوست ناقص: %s)", m.id, why)
            return
        asin = (ASIN_RE.search(text) or [None, None])[1]
        if asin:
            r = db.execute("SELECT 1 FROM fb_queue WHERE asin=? AND created>? LIMIT 1",
                           (asin, int(time.time()) - HOURS24)).fetchone()
            if r:
                log.info("skip duplicate %s", asin)
                return
        try:
            db.execute("INSERT INTO fb_queue(msg_id,asin,text,img,created) VALUES(?,?,?,?,?)",
                       (m.id, asin, text, img, int(time.time())))
            db.commit()
            log.info("queued msg %s", m.id)
        except sqlite3.IntegrityError:
            pass

    asyncio.create_task(worker())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
