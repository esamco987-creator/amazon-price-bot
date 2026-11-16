"""تاريخ أسعار المنتجات + إحصائيات بوت التتبع + قرار الشراء + الرسم البياني.

بيتخزن في ملف SQLite واحد (bot_data.db) فيمكن لأكتر من برنامج (بوت التتبع وبوت العروض) يسجّلوا فيه مع بعض.
كل الدوال هنا آمنة: لو حصل أي خطأ بترجع قيمة فاضية ومبتوقّفش البوت.
"""
import io
import os
import time
import sqlite3
import logging
import threading
from datetime import datetime

log = logging.getLogger("price_history")

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, "bot_data.db")
KEEP_DAYS = 180          # بنحتفظ بتاريخ الأسعار المدة دي
MIN_GAP = 6 * 3600       # نفس السعر ما بيتسجّلش تاني قبل المدة دي
DAY = 86400

_init_lock = threading.Lock()
_inited = {"ok": False}


def _con():
    c = sqlite3.connect(DB_FILE, timeout=15)
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return c


def init():
    if _inited["ok"]:
        return
    with _init_lock:
        if _inited["ok"]:
            return
        try:
            c = _con()
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS prices(asin TEXT, ts INTEGER, price REAL);
                CREATE INDEX IF NOT EXISTS ix_prices ON prices(asin, ts);
                CREATE TABLE IF NOT EXISTS users(chat_id TEXT PRIMARY KEY, name TEXT, username TEXT,
                                                 first_seen INTEGER, last_seen INTEGER);
                CREATE TABLE IF NOT EXISTS searches(ts INTEGER, chat_id TEXT, asin TEXT, title TEXT);
                CREATE INDEX IF NOT EXISTS ix_searches ON searches(ts);
                CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, chat_id TEXT, asin TEXT);
                CREATE INDEX IF NOT EXISTS ix_events ON events(ts, kind);
                """
            )
            c.execute("DELETE FROM prices WHERE ts < ?", (int(time.time()) - KEEP_DAYS * DAY,))
            c.commit()
            c.close()
            _inited["ok"] = True
        except Exception as e:
            log.warning("history init failed: %s", type(e).__name__)


# ---------------------------------------------------------------- prices
def record(asin, price, ts=None):
    """بيسجّل سعر. بيتجاهل لو نفس آخر سعر مسجّل ولسه بدري."""
    try:
        if not asin or not price or price <= 0:
            return False
        init()
        ts = int(ts or time.time())
        c = _con()
        row = c.execute("SELECT ts, price FROM prices WHERE asin=? ORDER BY ts DESC LIMIT 1", (asin,)).fetchone()
        if row and abs(row[1] - price) < 0.005 and ts - row[0] < MIN_GAP:
            c.close()
            return False
        c.execute("INSERT INTO prices(asin, ts, price) VALUES (?,?,?)", (asin, ts, float(price)))
        c.commit()
        c.close()
        return True
    except Exception as e:
        log.warning("record failed: %s", type(e).__name__)
        return False


def record_many(pairs):
    """pairs: [(asin, price), ...] — دفعة واحدة (بوت العروض بيستخدمها)."""
    n = 0
    for asin, price in pairs:
        if record(asin, price):
            n += 1
    return n


def history(asin, days=90):
    try:
        init()
        c = _con()
        rows = c.execute(
            "SELECT ts, price FROM prices WHERE asin=? AND ts>=? ORDER BY ts", (asin, int(time.time()) - days * DAY)
        ).fetchall()
        c.close()
        return [(int(t), float(p)) for t, p in rows]
    except Exception as e:
        log.warning("history failed: %s", type(e).__name__)
        return []


def stats(asin, days=90, now=None):
    """n, span_days, min, max, avg (متوسط موزون بالمدة), min_ts. أو None لو مفيش بيانات."""
    rows = history(asin, days)
    if not rows:
        return None
    now = now or time.time()
    prices = [p for _, p in rows]
    lo = min(prices)
    min_ts = next(t for t, p in rows if p == lo)
    total = 0.0
    weight = 0.0
    for i, (t, p) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else now
        w = max(end - t, 1)
        total += p * w
        weight += w
    return {
        "n": len(rows),
        "span_days": (now - rows[0][0]) / DAY,
        "first_ts": rows[0][0],
        "min": lo,
        "max": max(prices),
        "avg": total / weight if weight else sum(prices) / len(prices),
        "min_ts": min_ts,
    }


def verdict(asin, price, now=None):
    """(كود, نص). الكود: buy / good / wait / ok / learning."""
    st = stats(asin, 90, now)
    if not st or st["n"] < 3 or st["span_days"] < 2:
        return "learning", "⏳ لسه بجمع بيانات سعر المنتج ده، وقرار الشراء هيبقى أدق بعد كام يوم."
    avg, lo = st["avg"], st["min"]
    days = max(int(st["span_days"]), 1)
    if price <= lo * 1.01:
        return "buy", f"🟢 اشتري دلوقتي: ده أقل سعر سجلته للمنتج في آخر {days} يوم."
    diff = (avg - price) / avg * 100
    if diff >= 8:
        return "good", f"🟢 سعر كويس: أقل من متوسط سعره بحوالي {round(diff)}% (حسب آخر {days} يوم)."
    if diff <= -8:
        return "wait", f"🔴 استنى: أغلى من متوسط سعره بحوالي {round(-diff)}% (حسب آخر {days} يوم)."
    return "ok", f"🟡 سعر عادي: قريب من متوسط سعره (حسب آخر {days} يوم)."


def _fmt_money(v):
    return f"{int(v):,}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def chart(asin, title, price, now=None):
    """صورة رسم بياني لسعر المنتج بشعار القناة. JPEG بايتات أو None لو البيانات مش كفاية."""
    try:
        import deal_card as dc
        from PIL import Image, ImageDraw

        rows = history(asin, 90)
        now = int(now or time.time())
        if price and (not rows or rows[-1][1] != price or now - rows[-1][0] > 3600):
            rows = rows + [(now, float(price))]
        if len(rows) < 2 or not dc.ensure_font():
            return None
        W, H = dc.W, dc.H
        card = Image.new("RGB", (W, H), dc.WHITE)
        d = ImageDraw.Draw(card)
        d.rectangle([0, 0, W, 16], fill=dc.NAVY)
        d.rectangle([0, 16, W, 22], fill=dc.YELLOW)

        # العنوان
        tf = dc.font(40)
        for i, ln in enumerate(dc.wrap(d, title or "", tf, W - 120, 2)):
            dc.draw_text(d, (W - 60, 44 + i * 54), ln, tf, dc.INK, "ra")

        # منطقة الرسم
        L, R, T, B = 170, W - 70, 190, 520
        t0, t1 = rows[0][0], max(rows[-1][0], rows[0][0] + DAY)
        prices = [p for _, p in rows]
        lo, hi = min(prices), max(prices)
        pad = (hi - lo) * 0.18 if hi > lo else max(hi * 0.05, 1.0)
        ylo, yhi = lo - pad, hi + pad

        def X(t):
            return L + (t - t0) / (t1 - t0) * (R - L)

        def Y(p):
            return B - (p - ylo) / (yhi - ylo) * (B - T)

        gf = dc.font(26)
        for k in range(5):
            v = ylo + (yhi - ylo) * k / 4
            y = Y(v)
            d.line([L, y, R, y], fill=(228, 230, 235), width=2)
            d.text((L - 14, y), _fmt_money(v), font=gf, fill=dc.GRAY, anchor="rm")
        d.text((L, B + 14), datetime.fromtimestamp(t0).strftime("%d/%m"), font=gf, fill=dc.GRAY, anchor="la")
        d.text((R, B + 14), datetime.fromtimestamp(rows[-1][0]).strftime("%d/%m"), font=gf, fill=dc.GRAY, anchor="ra")

        # خط السعر (سلّم: السعر بيثبت لحد ما يتغيّر)
        pts = []
        for i, (t, p) in enumerate(rows):
            pts.append((X(t), Y(p)))
            if i + 1 < len(rows):
                pts.append((X(rows[i + 1][0]), Y(p)))
        poly = [(L, B)] + pts + [(pts[-1][0], B)]
        d.polygon(poly, fill=(255, 244, 214))
        d.line(pts, fill=dc.NAVY, width=6, joint="curve")

        # أقل سعر والسعر الحالي
        lo_t = next(t for t, p in rows if p == lo)
        for (t, p), col, label, up in (
            ((lo_t, lo), dc.GREEN, f"أقل سعر {_fmt_money(lo)}", False),
            ((rows[-1][0], rows[-1][1]), dc.RED, f"الحالي {_fmt_money(rows[-1][1])}", True),
        ):
            x, y = X(t), Y(p)
            d.ellipse([x - 12, y - 12, x + 12, y + 12], fill=col, outline=dc.WHITE, width=3)
            lf = dc.font(28)
            lw = int(dc.width(d, label, lf)) + 30
            if up:   # الحالي: فوق النقطة ومحاذي لحافة الرسم
                cx = min(x - lw / 2 + 12, R - lw / 2)
                cy = y - 46
            else:    # الأقل: على شمال النقطة (أو يمينها لو مفيش مكان)
                cx = x - lw / 2 - 24
                if cx - lw / 2 < L:
                    cx = x + lw / 2 + 24
                cy = y
            d.rounded_rectangle([cx - lw / 2, cy - 22, cx + lw / 2, cy + 22], radius=10, fill=col)
            dc.draw_text(d, (cx, cy), label, lf, dc.WHITE, "mm")

        # ملخص
        st = stats(asin, 90, now) or {}
        sf = dc.font(34)
        parts = f"الحالي: {_fmt_money(price or rows[-1][1])}   |   الأقل: {_fmt_money(lo)}   |   الأعلى: {_fmt_money(hi)}"
        dc.draw_text(d, (W // 2, 590), parts, sf, dc.INK, "mm")
        days = max(int(st.get("span_days", 0)), 1)
        dc.draw_text(d, (W // 2, 640), f"سعر المنتج على مدى آخر {days} يوم (بالجنيه)", dc.font(28), dc.MUTED, "mm")
        dc._footer(card, d)
        out = io.BytesIO()
        card.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("chart failed: %s %s", type(e).__name__, e)
        return None


# ---------------------------------------------------------------- users & stats
def touch_user(chat_id, name="", username=""):
    try:
        init()
        now = int(time.time())
        c = _con()
        c.execute(
            "INSERT INTO users(chat_id, name, username, first_seen, last_seen) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, username=excluded.username, last_seen=excluded.last_seen",
            (str(chat_id), name or "", username or "", now, now),
        )
        c.commit()
        c.close()
    except Exception as e:
        log.warning("touch_user failed: %s", type(e).__name__)


def log_search(chat_id, asin, title):
    try:
        init()
        c = _con()
        c.execute("INSERT INTO searches(ts, chat_id, asin, title) VALUES (?,?,?,?)",
                  (int(time.time()), str(chat_id), asin, (title or "")[:120]))
        c.commit()
        c.close()
    except Exception as e:
        log.warning("log_search failed: %s", type(e).__name__)


def log_event(kind, chat_id="", asin=""):
    try:
        init()
        c = _con()
        c.execute("INSERT INTO events(ts, kind, chat_id, asin) VALUES (?,?,?,?)",
                  (int(time.time()), kind, str(chat_id), asin))
        c.commit()
        c.close()
    except Exception as e:
        log.warning("log_event failed: %s", type(e).__name__)


def _midnight(now=None):
    lt = time.localtime(now or time.time())
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def summary(now=None):
    """أرقام لوحة الإحصائيات."""
    try:
        init()
        now = int(now or time.time())
        today, week = _midnight(now), now - 7 * DAY
        c = _con()
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        out = {
            "users": q("SELECT COUNT(*) FROM users"),
            "users_today": q("SELECT COUNT(*) FROM users WHERE first_seen>=?", today),
            "users_week": q("SELECT COUNT(*) FROM users WHERE first_seen>=?", week),
            "active_week": q("SELECT COUNT(*) FROM users WHERE last_seen>=?", week),
            "searches_today": q("SELECT COUNT(*) FROM searches WHERE ts>=?", today),
            "searches_week": q("SELECT COUNT(*) FROM searches WHERE ts>=?", week),
            "alerts_today": q("SELECT COUNT(*) FROM events WHERE kind='alert' AND ts>=?", today),
            "alerts_week": q("SELECT COUNT(*) FROM events WHERE kind='alert' AND ts>=?", week),
            "top_searched": c.execute(
                "SELECT asin, MAX(title), COUNT(*) n, COUNT(DISTINCT chat_id) u FROM searches WHERE ts>=? "
                "GROUP BY asin ORDER BY n DESC, u DESC LIMIT 8", (week,)).fetchall(),
            "products_with_history": q("SELECT COUNT(DISTINCT asin) FROM prices"),
        }
        c.close()
        return out
    except Exception as e:
        log.warning("summary failed: %s", type(e).__name__)
        return None
