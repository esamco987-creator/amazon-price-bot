"""مخزن أكواد الخصم لبوت الأكواد (coupon_bot.py).

- أكواد أمازون: بتتجمع تلقائي من صفحات المنتجات اللي بوتاتك بتقراها أصلاً (من غير طلبات زيادة على أمازون).
- كوبونات المنتجات (وفّر X% بالضغط على الكوبون): من بوت العروض وبوت التتبع.
- أكواد المواقع التانية: بتدخلها إنت بإيدك من البوت.
كله في نفس ملف bot_data.db. كل الدوال آمنة: لو حصل خطأ بترجع قيمة فاضية.
"""
import re
import time
import hashlib
import logging

from bs4 import BeautifulSoup

import price_history as ph

log = logging.getLogger("coupon_store")

HOUR, DAY = 3600, 86400
AUTO_CODE_DAYS = 3        # كود أمازون ما اتشافش من أكتر من كده: بيتشال من القايمة
CLIP_HOURS = 24           # كوبون منتج ما اتشافش من أكتر من كده: بيتشال
AMAZON = "أمازون"

_ready = {"ok": False}

AR_RE = re.compile(
    r"(?:أدخل|ادخل|ادخلي|أدخلي|استخدم|استخدمي|اكتب)\s*(?:الرمز|الكود|رمز|كود)(?:\s*الترويجي)?\s*[:：]?\s*([A-Za-z0-9]{4,20})"
)
EN_RE = re.compile(
    r"(?:enter|use|apply)\s+(?:the\s+)?(?:promo(?:tional)?\s+)?(?:code|promocode)\s*[:：]?\s*([A-Za-z0-9]{4,20})", re.I
)
NOISE = ("الشروط", "Terms", "Details", "تسجيل الدخول للاستفادة من الكوبون", "Sign in to save with coupon")


def _con():
    return ph._con()


def init():
    if _ready["ok"]:
        return
    try:
        c = _con()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS amazon_codes(code TEXT PRIMARY KEY, descr TEXT, first_seen INTEGER,
                                                    last_seen INTEGER, hits INTEGER);
            CREATE TABLE IF NOT EXISTS clip_coupons(asin TEXT PRIMARY KEY, title TEXT, coupon TEXT, price REAL,
                                                    last_seen INTEGER);
            CREATE TABLE IF NOT EXISTS manual_codes(id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, code TEXT,
                                                    descr TEXT, expires INTEGER, added INTEGER);
            CREATE TABLE IF NOT EXISTS cb_users(chat_id TEXT PRIMARY KEY, name TEXT, username TEXT,
                                                first_seen INTEGER, last_seen INTEGER);
            """
        )
        c.commit()
        c.close()
        _ready["ok"] = True
    except Exception as e:
        log.warning("coupon init failed: %s", type(e).__name__)


# ---------------------------------------------------------------- استخراج الأكواد من صفحة منتج
def _clean(txt):
    txt = re.sub(r"\s+", " ", txt or "").strip()
    for n in NOISE:
        txt = txt.replace(n, "")
    return re.sub(r"\s+", " ", txt).strip(" .،-|")


def _valid(code):
    if not re.search(r"[A-Za-z]", code):
        return False
    if code.islower():           # كلمة إنجليزي عادية مش كود
        return False
    return True


def _find(text):
    found = []
    for rx in (AR_RE, EN_RE):
        for m in rx.finditer(text):
            code = m.group(1).upper()
            if _valid(code) and code not in found:
                found.append(code)
    return found


def extract_codes(page):
    """يرجع [(الكود, الوصف)] من صفحة منتج أمازون. فاضية لو مفيش."""
    try:
        if not page or not _find(page):   # فحص سريع قبل التحليل الكامل
            return []
        soup = BeautifulSoup(page, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        codes = _find(soup.get_text(" "))
        out = []
        for code in codes:
            others = [c for c in codes if c != code]
            desc = ""
            for node in soup.find_all(string=re.compile(re.escape(code), re.I)):
                el = node.parent
                for _i in range(3):
                    txt = _clean(el.get_text(" ", strip=True))
                    if len(txt) >= 30 or el.parent is None:
                        break
                    nxt = _clean(el.parent.get_text(" ", strip=True))
                    if any(o.lower() in nxt.lower() for o in others):
                        break          # ما نخلطش وصف كود بوصف كود تاني
                    el = el.parent
                desc = _clean(el.get_text(" ", strip=True))[:220]
                if desc:
                    break
            out.append((code, desc))
        return out
    except Exception as e:
        log.warning("extract_codes failed: %s", type(e).__name__)
        return []


def harvest(page, asin=""):
    """يقرا الأكواد من صفحة ويسجلها. بيتنادى من بوت العروض وبوت التتبع على صفحة اتجابت أصلاً."""
    try:
        codes = extract_codes(page)
        if not codes:
            return 0
        init()
        now = int(time.time())
        c = _con()
        for code, desc in codes:
            c.execute(
                "INSERT INTO amazon_codes(code, descr, first_seen, last_seen, hits) VALUES (?,?,?,?,1) "
                "ON CONFLICT(code) DO UPDATE SET last_seen=excluded.last_seen, hits=hits+1, "
                "descr=CASE WHEN length(excluded.descr) > length(descr) THEN excluded.descr ELSE descr END",
                (code, desc, now, now),
            )
        c.commit()
        c.close()
        return len(codes)
    except Exception as e:
        log.warning("harvest failed: %s", type(e).__name__)
        return 0


def record_clip(asin, title, coupon, price=None):
    """كوبون على منتج (وفّر X% بالضغط على الكوبون)."""
    try:
        if not asin or not coupon:
            return
        init()
        c = _con()
        c.execute(
            "INSERT INTO clip_coupons(asin, title, coupon, price, last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET title=excluded.title, coupon=excluded.coupon, price=excluded.price, "
            "last_seen=excluded.last_seen",
            (asin, (title or "")[:120], coupon, price, int(time.time())),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("record_clip failed: %s", type(e).__name__)


# ---------------------------------------------------------------- استعلامات
def auto_codes(now=None):
    try:
        init()
        now = int(now or time.time())
        c = _con()
        rows = c.execute(
            "SELECT code, descr, last_seen FROM amazon_codes WHERE last_seen>=? ORDER BY last_seen DESC, hits DESC LIMIT 12",
            (now - AUTO_CODE_DAYS * DAY,),
        ).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def clip_items(now=None, limit=8):
    try:
        init()
        now = int(now or time.time())
        c = _con()
        rows = c.execute(
            "SELECT asin, title, coupon, price, last_seen FROM clip_coupons WHERE last_seen>=? "
            "ORDER BY last_seen DESC LIMIT ?",
            (now - CLIP_HOURS * HOUR, limit),
        ).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def manual_codes(site=None, now=None):
    """أكواد دخلتها بإيدك وما انتهتش. (id, site, code, descr, expires)"""
    try:
        init()
        now = int(now or time.time())
        c = _con()
        sql = "SELECT id, site, code, descr, expires FROM manual_codes WHERE (expires IS NULL OR expires>=?)"
        args = [now]
        if site:
            sql += " AND site=?"
            args.append(site)
        rows = c.execute(sql + " ORDER BY added DESC", args).fetchall()
        c.close()
        return rows
    except Exception:
        return []


def sites(now=None):
    """المواقع اللي ليها أكواد شغالة (من غير أمازون: ليه قسم لوحده)."""
    seen = []
    for _, site, *_ in manual_codes(now=now):
        if site != AMAZON and site not in seen:
            seen.append(site)
    return seen


def site_key(site):
    return hashlib.md5(site.encode("utf-8")).hexdigest()[:8]


def site_by_key(key, now=None):
    for s in sites(now):
        if site_key(s) == key:
            return s
    return None


def add_manual(site, code, descr, expires):
    try:
        init()
        c = _con()
        cur = c.execute(
            "INSERT INTO manual_codes(site, code, descr, expires, added) VALUES (?,?,?,?,?)",
            (site.strip(), code.strip(), (descr or "").strip(), expires, int(time.time())),
        )
        c.commit()
        rid = cur.lastrowid
        c.close()
        return rid
    except Exception as e:
        log.warning("add_manual failed: %s", type(e).__name__)
        return None


def delete_manual(rid):
    try:
        init()
        c = _con()
        c.execute("DELETE FROM manual_codes WHERE id=?", (int(rid),))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def all_manual():
    """للمالك: كل الأكواد حتى المنتهية."""
    try:
        init()
        c = _con()
        rows = c.execute("SELECT id, site, code, descr, expires FROM manual_codes ORDER BY added DESC LIMIT 40").fetchall()
        c.close()
        return rows
    except Exception:
        return []


# ---------------------------------------------------------------- مستخدمين وإحصائيات البوت
def touch_user(chat_id, name="", username=""):
    try:
        init()
        now = int(time.time())
        c = _con()
        c.execute(
            "INSERT INTO cb_users(chat_id, name, username, first_seen, last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, username=excluded.username, last_seen=excluded.last_seen",
            (str(chat_id), name or "", username or "", now, now),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("cb touch failed: %s", type(e).__name__)


def stats(now=None):
    try:
        init()
        ph.init()
        now = int(now or time.time())
        today, week = ph._midnight(now), now - 7 * DAY
        c = _con()
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        out = {
            "users": q("SELECT COUNT(*) FROM cb_users"),
            "users_today": q("SELECT COUNT(*) FROM cb_users WHERE first_seen>=?", today),
            "users_week": q("SELECT COUNT(*) FROM cb_users WHERE first_seen>=?", week),
            "active_week": q("SELECT COUNT(*) FROM cb_users WHERE last_seen>=?", week),
            "auto_codes": q("SELECT COUNT(*) FROM amazon_codes WHERE last_seen>=?", now - AUTO_CODE_DAYS * DAY),
            "clip_now": q("SELECT COUNT(*) FROM clip_coupons WHERE last_seen>=?", now - CLIP_HOURS * HOUR),
            "manual_live": q("SELECT COUNT(*) FROM manual_codes WHERE expires IS NULL OR expires>=?", now),
            "views": c.execute(
                "SELECT kind, COUNT(*) FROM events WHERE kind LIKE 'cb_%' AND ts>=? GROUP BY kind ORDER BY 2 DESC",
                (week,),
            ).fetchall(),
        }
        c.close()
        return out
    except Exception as e:
        log.warning("cb stats failed: %s", type(e).__name__)
        return None
