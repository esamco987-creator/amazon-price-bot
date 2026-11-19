"""بوت إعادة النشر: بيراجع البوستات اللي اتنشرت في القناة كل فترة، ولو المنتج لسه موجود وبنفس السعر
بيعيد نشرها (من أول القناة تاني). لو المنتج خلص أو السعر زاد بيوقف إعادتها، ولو السعر نزل بيبلّغك.
بيشتغل بتوكن بوت المسودات (DRAFT_BOT_TOKEN) لأنه أدمن في القناة.
إعدادات اختيارية من env.sh:
  REPOST_HOURS=2         كل قد إيه ساعة نعيد نفس البوست
  REPOST_MAX_AGE_H=24    مانعيدش بوست أقدم من كده
  REPOST_MAX_TIMES=3     أقصى عدد مرات إعادة للبوست
  REPOST_PER_CYCLE=2     أقصى عدد بوستات نعيدها في المرة
  REPOST_QUIET=1-8       ساعات هدوء (مفيش إعادة فيها)
  REPOST_DELETE_OLD=1    يمسح نسخة الإعادة القديمة لما ينزل نسخة جديدة (من غير البوست الأصلي)
"""
import os
import re
import time
import asyncio
import logging

from telegram import Bot, InputFile
from telegram.constants import ParseMode

import price_history as ph
import repost_store as rs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("repost")

TOKEN = os.environ.get("DRAFT_BOT_TOKEN", "")
CHANNEL = "@PromoCodeOnlain"
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0


def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


HOUR = 3600
MIN_GAP = _num("REPOST_HOURS", 2) * HOUR
MAX_AGE = _num("REPOST_MAX_AGE_H", 24) * HOUR
MAX_TIMES = int(_num("REPOST_MAX_TIMES", 3))
PER_CYCLE = int(_num("REPOST_PER_CYCLE", 2))
DELETE_OLD = os.environ.get("REPOST_DELETE_OLD", "1") != "0"
RETRY_GAP = 20 * 60          # لو صفحة أمازون مردتش: نجرب تاني بعد كده
MAX_TRIES = 4                # بعد كام محاولة فاشلة نوقف المراجعة للبوست
CYCLE_EVERY = 15 * 60
PRICE_TOL = 0.01             # فرق السعر المسموح (1%)
PREFIX = ""   # مفيش جملة قبل البوست المعاد
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "repost_log.txt")


def note(msg):
    log.info(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def quiet_now():
    spec = os.environ.get("REPOST_QUIET", "1-8")
    try:
        a, b = [int(x) for x in spec.split("-")]
    except Exception:
        return False
    h = time.localtime().tm_hour
    return (a <= h < b) if a < b else (h >= a or h < b)


# ---------------------------------------------------------------- فحص المنتج
def check_product(asin):
    """('ok', سعر) | ('gone', None) | ('unknown', None). unknown = أمازون ماردتش أو صفحة غير مفهومة (نجرب بعدين)."""
    import post_bot as pb
    dbg = []
    _, page = pb.get_page(f"https://www.amazon.eg/dp/{asin}", dbg)
    if not page or "productTitle" not in page:
        return "unknown", None
    title, price, old, rating, img = pb.read_product(page)
    if not title:
        return "unknown", None
    if not price:
        return "gone", None          # الصفحة موجودة ومفيش سعر = غير متاح
    return "ok", price


# ---------------------------------------------------------------- إرسال
async def channel_markup():
    try:
        import draft_bot
        return await draft_bot.channel_kb()
    except Exception as e:
        log.warning("buttons unavailable: %s", type(e).__name__)
        return None


async def send_repost(bot, row):
    asin, price, post, has_img, first_ts, last_ts, reposts, tries, last_msg = row
    text = PREFIX + post.rstrip()
    kb = await channel_markup()
    image = rs.image_of(asin) if has_img else None
    if image and len(text) <= 1000:
        m = await bot.send_photo(CHANNEL, photo=InputFile(image, filename="p.jpg"), caption=text,
                                 parse_mode=ParseMode.HTML, reply_markup=kb)
        return m.message_id
    if image:
        await bot.send_photo(CHANNEL, photo=InputFile(image, filename="p.jpg"))
    m = await bot.send_message(CHANNEL, text if len(text) <= 4000 else post, parse_mode=ParseMode.HTML,
                               reply_markup=kb, disable_web_page_preview=True)
    return m.message_id


async def tell_owner(bot, text):
    if not OWNER_ID:
        return
    try:
        await bot.send_message(OWNER_ID, text, disable_web_page_preview=True)
    except Exception as e:
        log.warning("owner notify failed: %s", type(e).__name__)


async def run_cycle(bot):
    now = int(time.time())
    if quiet_now():
        return
    rows = rs.due(now, MIN_GAP, MAX_AGE, MAX_TIMES, RETRY_GAP, PER_CYCLE * 3)
    done = 0
    for row in rows:
        if done >= PER_CYCLE:
            break
        asin, price, post, has_img, first_ts, last_ts, reposts, tries, last_msg = row
        state, cur = await asyncio.to_thread(check_product, asin)
        rs.update(asin, last_check=int(time.time()))
        if state == "unknown":
            tries += 1
            rs.update(asin, tries=tries)
            if tries >= MAX_TRIES:
                rs.update(asin, status="skipped", note="amazon not reachable")
                note(f"{asin}: skipped after {tries} failed checks")
            continue
        if state == "gone":
            rs.update(asin, status="gone", note="unavailable")
            note(f"{asin}: unavailable -> stopped")
            continue
        if cur > price * (1 + PRICE_TOL):
            rs.update(asin, status="changed", note=f"price up {price}->{cur}")
            note(f"{asin}: price up {price} -> {cur} -> stopped")
            continue
        if cur < price * (1 - PRICE_TOL):
            rs.update(asin, status="changed", note=f"price down {price}->{cur}")
            note(f"{asin}: price DOWN {price} -> {cur}")
            await tell_owner(bot, f"📉 السعر نزل على منتج في القناة: من {price:g} إلى {cur:g} جنيه.\n"
                                  f"https://www.amazon.eg/dp/{asin}\nجهّز بوست جديد بالسعر الجديد لو عايز.")
            continue
        try:
            mid = await send_repost(bot, row)
        except Exception as e:
            note(f"{asin}: repost failed: {type(e).__name__}")
            rs.update(asin, last_check=int(time.time()))
            continue
        if DELETE_OLD and last_msg:
            try:
                await bot.delete_message(CHANNEL, last_msg)
            except Exception:
                pass
        rs.update(asin, last_ts=int(time.time()), reposts=reposts + 1, last_msg=mid, tries=0)
        note(f"{asin}: reposted (#{reposts + 1}) price {cur:g}")
        done += 1
        await asyncio.sleep(4)


async def main():
    if not TOKEN:
        log.error("DRAFT_BOT_TOKEN مش موجود في env.sh")
        return
    ph.init()
    rs.init()
    note(f"repost bot on: every {MIN_GAP / HOUR:g}h, max {MAX_TIMES} times, quiet={os.environ.get('REPOST_QUIET', '1-8')}")
    async with Bot(TOKEN) as bot:
        while True:
            try:
                await run_cycle(bot)
            except Exception as e:
                note(f"cycle error: {type(e).__name__} {str(e)[:120]}")
            await asyncio.sleep(CYCLE_EVERY)


if __name__ == "__main__":
    asyncio.run(main())
