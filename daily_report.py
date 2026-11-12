"""
التقرير اليومي لصاحب القناة: المشتركين، المشاهدات، أحسن بوست، شغل البوتات، إحصائيات بوت البحث،
وآخر تقرير أمازون بعته (لو فيه). بيتبعت كل يوم الساعة REPORT_HOUR بتوقيت مصر.
لو عايز التقرير فوراً: اعمل ملف اسمه report_now.flag جنب البوت (touch report_now.flag).
"""
import os
import re
import json
import html
import time
import asyncio
import logging
import datetime

log = logging.getLogger("report")

BASE = os.path.dirname(os.path.abspath(__file__))
CHANNEL = "PromoCodeOnlain"
REPORT_HOUR = 22
REPORT_MIN = 5
SNAP_FILE = os.path.join(BASE, "channel_stats.json")
SCAN_LOG = os.path.join(BASE, "scan_log.txt")
AMAZON_FILE = os.path.join(BASE, "amazon_reports.json")
FLAG_FILE = os.path.join(BASE, "report_now.flag")
DAYS_AR = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Africa/Cairo")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=2))


def now():
    return datetime.datetime.now(tz())


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path, d):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        log.warning("save %s failed: %s", path, type(e).__name__)


def _signed(n):
    return f"+{n:,}" if n > 0 else (f"{n:,}" if n < 0 else "0")


def scanner_today():
    """بيقرا scan_log.txt ويعدّ شغل فاحص العروض النهارده."""
    today = now().strftime("%Y-%m-%d")
    rounds = sent = coupons = 0
    try:
        with open(SCAN_LOG, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith(today):
                    continue
                if " scan: " in line:
                    rounds += 1
                    m = re.search(r"sent=(\d+)", line)
                    sent += int(m.group(1)) if m else 0
                    m = re.search(r"coupons=(\d+)", line)
                    coupons += int(m.group(1)) if m else 0
    except Exception:
        pass
    return rounds, sent, coupons


def amazon_section():
    d = _load(AMAZON_FILE)
    if not d.get("latest"):
        return ""
    r = d["latest"]
    try:
        age = (now().date() - datetime.date.fromisoformat(r["saved"])).days
    except Exception:
        age = 99
    if age > 3:
        return ""
    return "💰 <b>أمازون</b> (آخر تقرير اتبعت " + html.escape(r.get("saved_label", "")) + ")\n" + r.get("text", "")


async def channel_part(client):
    from telethon.tl.functions.channels import GetFullChannelRequest
    ent = await client.get_entity(CHANNEL)
    full = await client(GetFullChannelRequest(ent))
    subs = full.full_chat.participants_count or 0

    snaps = _load(SNAP_FILE)
    today = now().strftime("%Y-%m-%d")
    snaps[today] = subs
    for k in sorted(snaps)[:-60]:
        snaps.pop(k, None)
    _save(SNAP_FILE, snaps)

    def delta(days):
        for back in range(days, days + 4):
            k = (now().date() - datetime.timedelta(days=back)).isoformat()
            if k in snaps:
                return subs - snaps[k]
        return None

    d1, d7 = delta(1), delta(7)
    parts = [f"👥 المشتركين: <b>{subs:,}</b>"]
    ch = []
    if d1 is not None:
        ch.append(f"{_signed(d1)} النهارده")
    if d7 is not None:
        ch.append(f"{_signed(d7)} آخر أسبوع")
    if ch:
        parts[0] += " (" + " • ".join(ch) + ")"

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    posts = []
    async for m in client.iter_messages(ent, limit=300):
        if m.date < since:
            break
        if m.message is None and not m.media:
            continue
        posts.append(m)
    views = sum((m.views or 0) for m in posts)
    parts.append(f"📝 بوستات آخر 24 ساعة: <b>{len(posts)}</b>" + (
        f" • مشاهدات: <b>{views:,}</b> (متوسط {views // len(posts):,})" if posts else ""))
    if posts:
        top = max(posts, key=lambda m: m.views or 0)
        first = re.sub(r"\s+", " ", (top.message or "").strip().split("\n")[0])[:60] or "بوست بصورة"
        parts.append(f"🏆 أحسن بوست: «{html.escape(first)}» — {(top.views or 0):,} مشاهدة")
        parts.append(f"https://t.me/{CHANNEL}/{top.id}")
    return "\n".join(parts)


async def build_report(client):
    n = now()
    lines = [f"🌙 <b>تقرير اليوم — {DAYS_AR[n.weekday()]} {n.strftime('%d/%m')}</b>", ""]
    lines.append("📢 <b>القناة</b>")
    try:
        lines.append(await channel_part(client))
    except Exception as e:
        log.warning("channel part failed: %s %s", type(e).__name__, str(e)[:100])
        lines.append("معرفتش أقرا بيانات القناة دلوقتي.")
    rounds, sent, coupons = scanner_today()
    lines += ["", "🤖 <b>فاحص عروض أمازون</b>",
              f"جولات الفحص: {rounds} • مسودات اتبعتت: <b>{sent}</b>" + (f" • فيها كوبون: {coupons}" if coupons else "")]
    try:
        import compare_bot as cb
        lines += ["", cb.stats_text()]
    except Exception as e:
        log.warning("search stats unavailable: %s", type(e).__name__)
    amz = amazon_section()
    if amz:
        lines += ["", amz]
    else:
        lines += ["", "💰 <b>أمازون:</b> ابعتلي ملف تقرير الأرباح (Earnings أو Orders) وأحوله لملخص."]
    return "\n".join(lines)[:4000]


def seconds_until_report():
    n = now()
    t = n.replace(hour=REPORT_HOUR, minute=REPORT_MIN, second=0, microsecond=0)
    if t <= n:
        t += datetime.timedelta(days=1)
    return (t - n).total_seconds()


async def loop(client, send):
    """send(text) -> بيبعت الرسالة (دالة عادية، بتتنادى في thread)."""
    await asyncio.sleep(30)
    next_at = time.time() + seconds_until_report()
    while True:
        try:
            if os.path.exists(FLAG_FILE):
                try:
                    os.remove(FLAG_FILE)
                except Exception:
                    pass
                text = await build_report(client)
                await asyncio.to_thread(send, text)
            elif time.time() >= next_at:
                text = await build_report(client)
                await asyncio.to_thread(send, text)
                next_at = time.time() + seconds_until_report()
        except Exception as e:
            log.warning("report loop error: %s %s", type(e).__name__, str(e)[:120])
            next_at = time.time() + 600
        await asyncio.sleep(20)
