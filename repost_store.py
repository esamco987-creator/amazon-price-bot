"""سجل البوستات اللي اتنشرت في القناة (لبوت إعادة النشر repost_bot.py).
كل دوال الملف آمنة: لو حصل خطأ بتتجاهله وما بتوقّفش النشر."""
import os
import re
import time
import html
import logging

import price_history as ph

log = logging.getLogger("repost_store")
BASE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(BASE, "reposts")
ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*جنيه")
_ready = {"ok": False}


def _con():
    return ph._con()


def init():
    if _ready["ok"]:
        return
    c = _con()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS channel_posts(
            asin TEXT PRIMARY KEY, price REAL, post TEXT, has_img INTEGER,
            first_ts INTEGER, last_ts INTEGER, reposts INTEGER DEFAULT 0,
            status TEXT DEFAULT 'live', last_check INTEGER DEFAULT 0, tries INTEGER DEFAULT 0,
            last_msg INTEGER, note TEXT);
        """
    )
    c.commit()
    c.close()
    _ready["ok"] = True


def parse_post(post):
    """(asin, price) من نص بوست. None لو مفيش لينك أمازون."""
    m = ASIN_RE.search(post or "")
    if not m:
        return None, None
    text = re.sub(r"<[^>]+>", " ", html.unescape(post))
    p = PRICE_RE.search(text)
    price = float(p.group(1).replace(",", "")) if p else None
    return m.group(1), price


def record(image, post):
    """بيتنادى بعد ما بوست اتنشر في القناة."""
    try:
        asin, price = parse_post(post)
        if not asin or not price:
            return
        init()
        now = int(time.time())
        if image:
            os.makedirs(IMG_DIR, exist_ok=True)
            with open(os.path.join(IMG_DIR, asin + ".jpg"), "wb") as f:
                f.write(image)
        c = _con()
        c.execute(
            "INSERT INTO channel_posts(asin, price, post, has_img, first_ts, last_ts, reposts, status) "
            "VALUES (?,?,?,?,?,?,0,'live') ON CONFLICT(asin) DO UPDATE SET price=excluded.price, post=excluded.post, "
            "has_img=excluded.has_img, first_ts=excluded.first_ts, last_ts=excluded.last_ts, reposts=0, "
            "status='live', tries=0, last_msg=NULL, note=NULL",
            (asin, price, post, 1 if image else 0, now, now),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("record failed: %s", type(e).__name__)


def due(now, min_gap, max_age, max_reposts, retry_gap, limit):
    """بوستات محتاجة مراجعة: عدّى عليها min_gap من آخر نشر، وأصغر من max_age، ومش اتكررت كتير."""
    try:
        init()
        c = _con()
        rows = c.execute(
            "SELECT asin, price, post, has_img, first_ts, last_ts, reposts, tries, last_msg FROM channel_posts "
            "WHERE status='live' AND last_ts<=? AND first_ts>=? AND reposts<? AND last_check<=? "
            "ORDER BY last_ts ASC LIMIT ?",
            (now - min_gap, now - max_age, max_reposts, now - retry_gap, limit),
        ).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def image_of(asin):
    try:
        with open(os.path.join(IMG_DIR, asin + ".jpg"), "rb") as f:
            return f.read()
    except Exception:
        return None


def update(asin, **kw):
    try:
        init()
        sets = ", ".join(f"{k}=?" for k in kw)
        c = _con()
        c.execute(f"UPDATE channel_posts SET {sets} WHERE asin=?", (*kw.values(), asin))
        c.commit()
        c.close()
    except Exception as e:
        log.warning("update failed: %s", type(e).__name__)


def stats(now=None):
    try:
        init()
        now = int(now or time.time())
        c = _con()
        out = dict(c.execute("SELECT status, COUNT(*) FROM channel_posts GROUP BY status").fetchall())
        out["reposts_total"] = c.execute("SELECT COALESCE(SUM(reposts),0) FROM channel_posts").fetchone()[0]
        c.close()
        return out
    except Exception:
        return {}
