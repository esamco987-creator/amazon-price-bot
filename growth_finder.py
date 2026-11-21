"""Telegram Growth Hunter - المرحلة 1: اكتشاف قنوات العروض وترتيبها.

بيدوّر في تليجرام على قنوات عامة بكلمات عروض/خصومات، يجمع المشتركين ومتوسط
المشاهدات وعدد البوستات يومياً، يحفظهم في bot_data.db، ويبعتلك أحسن القنوات
في رسالة على تليجرام. بيقرا بيانات القنوات العامة بس.

التشغيل:  python growth_finder.py
المطلوب في env.sh: API_ID, API_HASH, DRAFT_BOT_TOKEN, OWNER_ID
ملف الجلسة: growth.session (نسخة من session.session بتاعة بوت التحويل)
"""
import os, re, json, time, sqlite3, asyncio, logging, html, urllib.request, urllib.parse

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("growth")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "bot_data.db")
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TOKEN = os.environ.get("DRAFT_BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
MY_CHANNEL = "promocodeonlain"
TOP_N = int(os.environ.get("GROWTH_TOP", "15"))
MIN_SUBS = int(os.environ.get("GROWTH_MIN_SUBS", "300"))

KEYWORDS = [
    "عروض أمازون", "عروض امازون مصر", "كوبونات أمازون", "خصومات امازون",
    "عروض نون", "كوبونات نون", "خصومات نون", "كود خصم نون",
    "عروض جوميا", "كوبونات جوميا", "خصومات جوميا",
    "عروض وخصومات", "عروض مصر", "اكواد خصم مصر", "تخفيضات مصر",
    "صيد العروض", "صفقات مصر", "أوفر", "هميزات", "بلاك فرايدي مصر",
    "amazon egypt deals", "noon egypt deals", "jumia egypt deals", "egypt coupons",
]


def con():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS growth_channels(
        username TEXT PRIMARY KEY, title TEXT, subs INTEGER, avg_views INTEGER,
        posts_per_day REAL, score REAL, keyword TEXT, found_ts INTEGER, updated_ts INTEGER)""")
    return c


def score_of(subs, avg_views, ppd):
    if not subs:
        return 0.0
    er = min(avg_views / subs, 1.0)            # نسبة المشاهدة للمشتركين
    act = min(ppd / 10.0, 1.0)                 # نشاط (حد أقصى 10 بوست/يوم)
    size = min(subs / 50000.0, 1.0)            # حجم الجمهور
    return round(100 * (0.4 * size + 0.4 * er + 0.2 * act), 1)


async def stats(client, ch):
    full = await client(functions.channels.GetFullChannelRequest(ch))
    subs = full.full_chat.participants_count or 0
    msgs = await client.get_messages(ch, limit=20)
    views = [m.views for m in msgs if m and m.views]
    avg = int(sum(views) / len(views)) if views else 0
    ppd = 0.0
    dates = [m.date for m in msgs if m and m.date]
    if len(dates) >= 2:
        span = (dates[0] - dates[-1]).total_seconds() / 86400
        ppd = round(len(dates) / max(span, 0.25), 1)
    return subs, avg, ppd


def tg_send(text):
    if not (TOKEN and OWNER_ID):
        print(text)
        return
    data = urllib.parse.urlencode({"chat_id": OWNER_ID, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=30)
    except Exception as e:
        log.warning("send failed: %s", e)


async def main():
    client = TelegramClient(os.path.join(BASE, "growth"), API_ID, API_HASH)
    await client.start()
    db = con()
    seen = {}
    for kw in KEYWORDS:
        try:
            res = await client(functions.contacts.SearchRequest(q=kw, limit=50))
        except FloodWaitError as e:
            log.warning("flood wait %ss, stop searching", e.seconds)
            break
        except Exception as e:
            log.warning("search %s: %s", kw, e)
            continue
        for c in res.chats:
            if isinstance(c, types.Channel) and c.broadcast and c.username \
                    and c.username.lower() != MY_CHANNEL and c.username not in seen:
                seen[c.username] = (c, kw)
        await asyncio.sleep(2)
    log.info("found %d channels", len(seen))

    now = int(time.time())
    for i, (u, (c, kw)) in enumerate(seen.items(), 1):
        try:
            subs, avg, ppd = await stats(client, c)
        except FloodWaitError as e:
            log.warning("flood wait %ss, stopping", e.seconds)
            break
        except Exception as e:
            log.info("skip %s: %s", u, e)
            continue
        if subs < MIN_SUBS:
            continue
        sc = score_of(subs, avg, ppd)
        db.execute("""INSERT INTO growth_channels VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET title=excluded.title, subs=excluded.subs,
            avg_views=excluded.avg_views, posts_per_day=excluded.posts_per_day,
            score=excluded.score, updated_ts=excluded.updated_ts""",
                   (u, c.title, subs, avg, ppd, sc, kw, now, now))
        db.commit()
        log.info("%d/%d %s subs=%s views=%s ppd=%s score=%s", i, len(seen), u, subs, avg, ppd, sc)
        await asyncio.sleep(3)
    await client.disconnect()

    rows = db.execute("SELECT username,title,subs,avg_views,posts_per_day,score FROM growth_channels "
                      "ORDER BY score DESC LIMIT ?", (TOP_N,)).fetchall()
    total = db.execute("SELECT COUNT(*) FROM growth_channels").fetchone()[0]
    lines = [f"🔎 <b>قنوات المنافسين</b> (اتسجل {total} قناة)\n"]
    for n, (u, t, s, a, p, sc) in enumerate(rows, 1):
        lines.append(f"{n}. <a href=\"https://t.me/{u}\">{html.escape(t or u)}</a>\n"
                     f"   👥 {s:,} | 👁 {a:,} | 📝 {p}/يوم | ⭐ {sc}")
    tg_send("\n".join(lines) if rows else "ملقيتش قنوات النهاردة، جرب تاني بعد شوية.")


if __name__ == "__main__":
    asyncio.run(main())
