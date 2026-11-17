"""بوت أكواد الخصم: أكواد وكوبونات أمازون (تلقائي) + أكواد المواقع التانية (بتدخلها إنت).

المالك (OWNER_ID) بس هو اللي يقدر يستخدم: /add  /codes  /stats
"""
import os
import re
import time
import html
import asyncio
import logging
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

import price_history as ph
import coupon_store as cs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("coupon_bot")

TOKEN = os.environ.get("COUPON_BOT_TOKEN", "")
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
except ValueError:
    OWNER_ID = 0
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@PromoCodeOnlain")
CHANNEL_URL = "https://t.me/" + REQUIRED_CHANNEL.lstrip("@") if REQUIRED_CHANNEL else ""
AFFILIATE_TAG = "mixdeals-21"
AMAZON = cs.AMAZON
DEFAULT_SITES = [AMAZON, "نون", "جوميا"]
SITE_URLS = {"نون": "https://www.noon.com/egypt-ar/", "جوميا": "https://www.jumia.com.eg/"}

MAX_ITEMS_PER_SCREEN = 10


def esc(x):
    return html.escape(str(x or ""), quote=False)


def amazon_home():
    return f"https://www.amazon.eg/?tag={AFFILIATE_TAG}"


def amazon_dp(asin):
    return f"https://www.amazon.eg/dp/{asin}?tag={AFFILIATE_TAG}"


def money(v):
    try:
        return f"{int(round(float(v))):,} جنيه"
    except Exception:
        return ""


def day_str(ts):
    return time.strftime("%d/%m", time.localtime(ts))


def age_text(ts, now=None):
    mins = max(int(((now or time.time()) - ts) / 60), 0)
    if mins < 60:
        return "اتشاف من شوية"
    hours = mins // 60
    if hours < 24:
        return f"اتشاف من {hours} ساعة"
    return f"اتشاف من {hours // 24} يوم"


# ---------------------------------------------------------------- مشترك في القناة؟
_member_cache = {}
_warned = {"m": False}


async def is_member(bot, uid, force=False):
    """لو البوت مش أدمن في القناة (مايقدرش يتأكد) بنسمح بدل ما نوقّف البوت."""
    if not REQUIRED_CHANNEL or uid == OWNER_ID:
        return True
    hit = _member_cache.get(uid)
    if hit and hit[1] and not force and time.time() - hit[0] < 600:
        return True
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        ok = m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", True))
        _member_cache[uid] = (time.time(), ok)
        return ok
    except Exception as e:
        if not _warned["m"]:
            _warned["m"] = True
            log.warning("membership check failed (%s): خلي البوت أدمن في القناة", type(e).__name__)
        return True


def gate_kb():
    rows = []
    if CHANNEL_URL:
        rows.append([Btn("📢 اشترك في القناة", url=CHANNEL_URL)])
    rows.append([Btn("✅ اشتركت، كمّل", callback_data="sub")])
    return Kb(rows)


GATE_TEXT = "عشان تشوف أكواد الخصم لازم تكون مشترك في قناة عروض وخصومات 📢\nاشترك الأول، وبعدين اضغط «✅ اشتركت، كمّل»."


# ---------------------------------------------------------------- الشاشات
def main_view():
    rows = [[Btn("🛒 أمازون: أكواد وكوبونات", callback_data="az")]]
    sites = cs.sites()
    for i in range(0, len(sites), 2):
        rows.append([Btn(f"🛍️ {s}", callback_data=f"s:{cs.site_key(s)}") for s in sites[i:i + 2]])
    if CHANNEL_URL:
        rows.append([Btn("📢 قناة عروض وخصومات", url=CHANNEL_URL)])
    return "🎟️ <b>أكواد الخصم</b>\n\nاختار الموقع اللي عايز أكواد خصمه 👇", Kb(rows)


def amazon_menu_view():
    kb = Kb([
        [Btn("🎟️ أكواد الخصم", callback_data="azc")],
        [Btn("🏷️ منتجات عليها كوبون", callback_data="azp")],
        [Btn("⬅️ رجوع", callback_data="m")],
    ])
    return "🛒 <b>أمازون</b>\n\n🎟️ <b>أكواد الخصم:</b> أكواد بتكتبها وقت الدفع (بنوك وعروض)\n🏷️ <b>منتجات عليها كوبون:</b> بتضغط على الكوبون في صفحة المنتج", kb


def code_block(code, descr, extra=""):
    lines = [f"🔹 <code>{esc(code)}</code>"]
    if descr:
        lines.append(esc(descr))
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def amazon_codes_view(now=None):
    blocks = []
    for _id, _site, code, descr, exp in cs.manual_codes(AMAZON, now):
        blocks.append(code_block(code, descr, f"⏳ ينتهي: {day_str(exp)}" if exp else ""))
    seen = {b.split("</code>")[0] for b in blocks}
    for code, descr, last in cs.auto_codes(now):
        if f"🔹 <code>{esc(code)}" in seen:
            continue
        blocks.append(code_block(code, descr, "🕒 " + age_text(last, now)))
    blocks = blocks[:MAX_ITEMS_PER_SCREEN]
    kb = Kb([[Btn("🛒 افتح أمازون", url=amazon_home())], [Btn("⬅️ رجوع", callback_data="az")]])
    if not blocks:
        return ("🎟️ <b>أكواد أمازون</b>\n\nمفيش أكواد متاحة دلوقتي. بنحدّث القايمة أول بأول، جرّب بعد شوية 🙏", kb)
    text = (
        "🎟️ <b>أكواد خصم أمازون</b>\n(اضغط على الكود عشان تنسخه)\n\n"
        + "\n\n".join(blocks)
        + "\n\nℹ️ الشروط (البنك أو الحد الأدنى للشراء) مكتوبة في صفحة المنتج. لو الكود مشتغلش يبقى انتهى."
    )
    return text, kb


def amazon_products_view(now=None):
    items = cs.clip_items(now)
    kb = Kb([[Btn("🛒 افتح أمازون", url=amazon_home())], [Btn("⬅️ رجوع", callback_data="az")]])
    if not items:
        return ("🏷️ <b>منتجات عليها كوبون</b>\n\nمفيش منتجات عليها كوبون دلوقتي. جرّب بعد شوية 🙏", kb)
    lines = ["🏷️ <b>منتجات عليها كوبون خصم</b>\n(بتضغط على «الكوبون» في صفحة المنتج قبل ما تشتري)\n"]
    for i, (asin, title, coupon, price, last) in enumerate(items, 1):
        l = f"{i}. <a href=\"{amazon_dp(asin)}\">{esc((title or asin)[:70])}</a>\n   🎟️ {esc(coupon)}"
        if price:
            l += f" | 💰 {money(price)}"
        lines.append(l)
    return "\n\n".join(lines), kb


def site_view(site, now=None):
    rows = cs.manual_codes(site, now)[:MAX_ITEMS_PER_SCREEN]
    btns = []
    if SITE_URLS.get(site):
        btns.append([Btn(f"🔗 افتح {site}", url=SITE_URLS[site])])
    btns.append([Btn("⬅️ رجوع", callback_data="m")])
    if not rows:
        return f"🛍️ <b>{esc(site)}</b>\n\nمفيش أكواد متاحة دلوقتي 🙏", Kb(btns)
    blocks = [code_block(code, descr, f"⏳ ينتهي: {day_str(exp)}" if exp else "") for _i, _s, code, descr, exp in rows]
    return f"🎟️ <b>أكواد خصم {esc(site)}</b>\n(اضغط على الكود عشان تنسخه)\n\n" + "\n\n".join(blocks), Kb(btns)


async def show(bot, chat, view, q=None):
    text, kb = view
    kw = dict(parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
    if q is not None and q.message:
        try:
            await q.edit_message_text(text, **kw)
            return
        except Exception:
            pass
    await bot.send_message(chat, text, **kw)


async def safe_answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except Exception:
        pass


def note(kind, chat):
    ph.log_event(kind, chat, "")


# ---------------------------------------------------------------- أوامر عامة
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u, chat = update.effective_user, update.effective_chat.id
    cs.touch_user(chat, (u.full_name if u else "") or "", (u.username or "") if u else "")
    if not await is_member(context.bot, u.id):
        await update.message.reply_text(GATE_TEXT, reply_markup=gate_kb())
        return
    note("cb_start", chat)
    await show(context.bot, chat, main_view())
    if u.id == OWNER_ID:
        await update.message.reply_text("أوامر المالك:\n/add — إضافة كود\n/codes — إدارة الأكواد\n/stats — الإحصائيات")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    chat = q.message.chat_id if q.message else q.from_user.id
    uid = q.from_user.id
    bot = context.bot
    act, _, arg = data.partition(":")

    if act == "sub":
        if not await is_member(bot, uid, force=True):
            await safe_answer(q, "لسه مش مشترك في القناة ❌ اشترك الأول وبعدين اضغط تاني", True)
            return
        await safe_answer(q, "تمام ✅")
        await show(bot, chat, main_view())
        return

    # شاشات المالك
    if act in ("as", "ad", "ae", "dl"):
        if uid != OWNER_ID:
            await safe_answer(q)
            return
        await owner_button(q, context, act, arg, chat)
        return

    if not await is_member(bot, uid):
        await safe_answer(q)
        await bot.send_message(chat, GATE_TEXT, reply_markup=gate_kb())
        return
    await safe_answer(q)
    if act == "m":
        note("cb_menu", chat)
        await show(bot, chat, main_view(), q)
    elif act == "az":
        await show(bot, chat, amazon_menu_view(), q)
    elif act == "azc":
        note("cb_amazon_codes", chat)
        await show(bot, chat, amazon_codes_view(), q)
    elif act == "azp":
        note("cb_amazon_products", chat)
        await show(bot, chat, amazon_products_view(), q)
    elif act == "s":
        site = cs.site_by_key(arg)
        if not site:
            await show(bot, chat, main_view(), q)
            return
        note(f"cb_site:{site[:20]}", chat)
        await show(bot, chat, site_view(site), q)


# ---------------------------------------------------------------- المالك: إضافة وإدارة الأكواد
def parse_expiry(text, now=None):
    """DD/MM أو DD/MM/YYYY: آخر اليوم ده. أو None لو مش تاريخ."""
    m = re.fullmatch(r"\s*(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\s*", (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    now = now or time.time()
    lt = time.localtime(now)
    y = int(m.group(3)) if m.group(3) else lt.tm_year
    if y < 100:
        y += 2000
    try:
        ts = int(time.mktime((y, mo, d, 23, 59, 0, 0, 0, -1)))
    except (ValueError, OverflowError):
        return None
    if not m.group(3) and ts < now:
        try:
            ts = int(time.mktime((y + 1, mo, d, 23, 59, 0, 0, 0, -1)))
        except (ValueError, OverflowError):
            return None
    return ts


def end_of_day(days, now=None):
    now = now or time.time()
    lt = time.localtime(now + days * 86400)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 59, 0, 0, 0, -1)))


def exp_kb():
    return Kb([
        [Btn("النهارده", callback_data="ae:0"), Btn("3 أيام", callback_data="ae:3")],
        [Btn("أسبوع", callback_data="ae:7"), Btn("شهر", callback_data="ae:30")],
        [Btn("من غير تاريخ", callback_data="ae:none")],
    ])


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    opts = list(dict.fromkeys(DEFAULT_SITES + cs.sites()))
    context.user_data["add"] = {"step": "site", "opts": opts}
    rows = [[Btn(s, callback_data=f"as:{i}") for i, s in list(enumerate(opts))[j:j + 3]] for j in range(0, len(opts), 3)]
    await update.message.reply_text("الكود ده لأنهي موقع؟ اختار، أو اكتب اسم موقع تاني:", reply_markup=Kb(rows))


async def owner_button(q, context, act, arg, chat):
    st = context.user_data.get("add")
    await safe_answer(q)
    bot = context.bot
    if act == "as" and st and st.get("step") == "site":
        try:
            st["site"] = st["opts"][int(arg)]
        except (ValueError, IndexError):
            return
        st["step"] = "code"
        await bot.send_message(chat, f"تمام: {st['site']}\nابعتلي الكود:")
    elif act == "ad" and st and st.get("step") == "descr":
        st["descr"], st["step"] = "", "exp"
        await bot.send_message(chat, "الكود ده بينتهي إمتى؟ اختار، أو اكتب التاريخ (مثال 31/12):", reply_markup=exp_kb())
    elif act == "ae" and st and st.get("step") == "exp":
        await finish_add(bot, chat, context, None if arg == "none" else end_of_day(int(arg)))
    elif act == "dl":
        cs.delete_manual(arg)
        await show(bot, chat, codes_view(), q)


async def finish_add(bot, chat, context, expires):
    st = context.user_data.pop("add", None)
    if not st:
        return
    rid = cs.add_manual(st["site"], st["code"], st.get("descr", ""), expires)
    if not rid:
        await bot.send_message(chat, "معرفتش أحفظ الكود 😕 جرب تاني.")
        return
    exp_line = f"\n⏳ ينتهي: {day_str(expires)}" if expires else "\n⏳ من غير تاريخ انتهاء"
    await bot.send_message(
        chat,
        f"✅ اتضاف في {esc(st['site'])}:\n\n{code_block(st['code'], st.get('descr', ''))}{exp_line}\n\n"
        "إضافة كود تاني: /add\nإدارة الأكواد: /codes",
        parse_mode=ParseMode.HTML,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id != OWNER_ID:
        await update.message.reply_text("اختار من الأزرار 👇 /start")
        return
    st = context.user_data.get("add")
    text = (update.message.text or "").strip()
    chat = update.effective_chat.id
    if not st:
        await update.message.reply_text("لإضافة كود: /add\nلإدارة الأكواد: /codes\nللإحصائيات: /stats")
        return
    if st["step"] == "site":
        st["site"], st["step"] = text[:30], "code"
        await update.message.reply_text(f"تمام: {st['site']}\nابعتلي الكود:")
    elif st["step"] == "code":
        st["code"], st["step"] = text.replace(" ", "")[:40], "descr"
        await update.message.reply_text(
            "اكتب وصف الكود (مثال: خصم 20% على الأزياء)، أو اضغط تخطي:",
            reply_markup=Kb([[Btn("تخطي", callback_data="ad:skip")]]),
        )
    elif st["step"] == "descr":
        st["descr"], st["step"] = text[:200], "exp"
        await update.message.reply_text("الكود ده بينتهي إمتى؟ اختار، أو اكتب التاريخ (مثال 31/12):", reply_markup=exp_kb())
    elif st["step"] == "exp":
        ts = parse_expiry(text)
        if ts is None:
            await update.message.reply_text("مفهمتش التاريخ. اكتبه بالشكل ده: 31/12 أو اختار من الأزرار:", reply_markup=exp_kb())
            return
        await finish_add(context.bot, chat, context, ts)


def codes_view(now=None):
    now = int(now or time.time())
    rows = cs.all_manual()
    if not rows:
        return "مفيش أكواد مضافة. أضف كود بـ /add", Kb([])
    lines, btns = ["🗂️ <b>الأكواد اللي أضفتها</b>\n"], []
    for rid, site, code, descr, exp in rows[:20]:
        dead = bool(exp) and exp < now
        tail = " (منتهي)" if dead else (f" — ينتهي {day_str(exp)}" if exp else "")
        lines.append(f"{rid}. {esc(site)} — <code>{esc(code)}</code>{tail}")
        btns.append([Btn(f"🗑️ حذف {code[:16]} ({site[:12]})", callback_data=f"dl:{rid}")])
    return "\n".join(lines), Kb(btns)


async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await show(context.bot, update.effective_chat.id, codes_view())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {uid}\nحطه في OWNER_ID (في ملف env.sh) وشغّل البوتات تاني.")
        return
    if uid != OWNER_ID:
        return
    s = await asyncio.to_thread(cs.stats)
    if not s:
        await update.message.reply_text("معرفتش أقرا الإحصائيات دلوقتي.")
        return
    names = {"cb_start": "فتح البوت", "cb_menu": "القايمة الرئيسية", "cb_amazon_codes": "أكواد أمازون",
             "cb_amazon_products": "منتجات عليها كوبون"}
    lines = [
        "📊 إحصائيات بوت الأكواد", "",
        f"👥 المستخدمين: {s['users']:,}",
        f"   جداد النهارده: {s['users_today']} | آخر 7 أيام: {s['users_week']} | نشطين آخر 7 أيام: {s['active_week']}", "",
        f"🎟️ أكواد أمازون المتاحة دلوقتي (اتجمعت تلقائي): {s['auto_codes']}",
        f"🏷️ منتجات عليها كوبون دلوقتي: {s['clip_now']}",
        f"🛍️ أكواد أضفتها وشغالة: {s['manual_live']}",
    ]
    if s["views"]:
        lines += ["", "👀 الأكتر استخدامًا (آخر 7 أيام):"]
        for kind, n in s["views"][:8]:
            label = names.get(kind) or (("موقع " + kind.split(":", 1)[1]) if kind.startswith("cb_site:") else kind)
            lines.append(f"• {label}: {n}")
    await update.message.reply_text("\n".join(lines)[:4000])


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


def main():
    if not TOKEN:
        print("COUPON_BOT_TOKEN مش موجود. اعمل export الأول.")
        return
    cs.init()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (
        Application.builder().token(TOKEN).concurrent_updates(True)
        .connect_timeout(20).read_timeout(40).write_timeout(40).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("codes", codes_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("Coupon bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
