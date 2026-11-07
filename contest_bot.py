import os
import re
import json
import html
import time
import random
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("contest_bot")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Africa/Cairo")
except Exception:
    TZ = timezone(timedelta(hours=2))

TOKEN = os.environ.get("CONTEST_BOT_TOKEN", "")
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

CHANNEL = "@PromoCodeOnlain"
CHANNEL_LINK = "https://t.me/PromoCodeOnlain"
MIN_REFS = 20  # أقل عدد أصحاب لازم الشخص يدعيهم ويشتركوا عشان يتأهل للسحب
BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, "contests.json")

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


# ---------------------------------------------------------------- storage
def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("next", 1)
    d.setdefault("contests", {})
    return d


DB = load_db()


def save_db():
    tmp = DB_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False)
        os.replace(tmp, DB_FILE)
    except Exception as e:
        log.warning("save_db failed: %s", type(e).__name__)


# ---------------------------------------------------------------- helpers
def fmt_time(ts):
    dt = datetime.fromtimestamp(ts, TZ)
    h = dt.hour
    suffix = "ص" if h < 12 else "م"
    return f"{dt:%d/%m/%Y} الساعة {h % 12 or 12}:{dt:%M} {suffix}"


def status_text(c, uid):
    e = c["entries"].get(str(uid))
    if not e:
        return ""
    need = c.get("min_refs", MIN_REFS)
    r = len(e["refs"])
    total = sum(len(x["refs"]) for x in c["entries"].values() if len(x["refs"]) >= need)
    lines = [f"👥 عدد اللي دعيتهم: {r} من {need} مطلوبين"]
    if r >= need and total:
        lines.append("✅ إنت مؤهل للسحب")
        lines.append(f"🎯 نسبة فرصتك من إجمالي المؤهلين: {r / total * 100:.1f}%")
    else:
        lines.append(f"⏳ لسه محتاج {need - r} دعوة عشان تتأهل للسحب")
        lines.append("🎯 نسبة فرصتك دلوقتي: 0%")
    return "\n".join(lines)


def parse_number(text):
    m = re.search(r"\d+(?:\.\d+)?", (text or "").translate(AR_DIGITS))
    return float(m.group(0)) if m else None


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name or "مشارك")}</a>'


async def is_member(bot, uid):
    m = await bot.get_chat_member(CHANNEL, uid)
    return m.status in ("member", "administrator", "creator", "restricted")


def contest_text(c):
    return (
        "🎁 <b>مسابقة جديدة!</b>\n\n"
        f"🏆 الجايزة: <b>{html.escape(c['prize'])}</b>\n"
        f"👥 عدد الفايزين: {c['winners']}\n"
        f"⏰ آخر معاد: {fmt_time(c['end_ts'])}\n\n"
        "📋 طريقة المشاركة:\n"
        "1️⃣ اشترك في قناة عروض وخصومات\n"
        "2️⃣ دوس على زر \"🎁 شارك دلوقتي\" تحت\n"
        "3️⃣ البوت هيديك لينك خاص بيك، ابعته لأصحابك\n"
        f"⚠️ عشان تتأهل للسحب لازم تدعي <b>{c.get('min_refs', MIN_REFS)} صديق</b> على الأقل "
        "يدخلوا من لينكك وهم مشتركين في القناة. وكل ما تدعي أكتر، فرصتك في الفوز بتزيد 🔥\n\n"
        "📢 قناة عروض وخصومات\n"
        f"{CHANNEL_LINK}"
    )


def join_kb(username, cid):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁 شارك دلوقتي", url=f"https://t.me/{username}?start=c{cid}")]]
    )


def me_kb(cid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📊 فرصي دلوقتي", callback_data=f"me:{cid}")]])


def personal_link(context, cid, uid):
    return f"https://t.me/{context.bot_data['username']}?start=c{cid}_r{uid}"


# ---------------------------------------------------------------- participants
async def handle_join(context, user, cid, ref):
    async def send(text, **kw):
        try:
            await context.bot.send_message(user.id, text, **kw)
        except Exception as e:
            log.warning("send failed: %s", type(e).__name__)

    c = DB["contests"].get(str(cid))
    if not c:
        await send("المسابقة دي مش موجودة.")
        return
    if c["status"] != "open" or time.time() >= c["end_ts"]:
        await send("المسابقة دي خلصت 🙏 تابع القناة للمسابقات الجاية.")
        return

    try:
        ok = await is_member(context.bot, user.id)
    except Exception as e:
        log.warning("membership check failed: %s", type(e).__name__)
        await send("حصلت مشكلة في التحقق من الاشتراك، جرب بعد شوية.")
        return

    if not ok:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_LINK)],
                [InlineKeyboardButton("✅ اشتركت، تحقق", callback_data=f"chk:{cid}:{ref or 0}")],
            ]
        )
        await send("لازم تشترك في قناة عروض وخصومات الأول عشان تدخل المسابقة 👇", reply_markup=kb)
        return

    uid = str(user.id)
    entries = c["entries"]
    link = personal_link(context, cid, user.id)

    if uid in entries:
        await send(
            "إنت مشارك بالفعل ✅\n\n"
            f"{status_text(c, user.id)}\n\n"
            "ابعت لينكك لأصحابك:\n"
            f"{link}",
            disable_web_page_preview=True,
            reply_markup=me_kb(cid),
        )
        return

    entries[uid] = {"name": user.full_name or "مشارك", "refs": []}
    if ref and ref != user.id and str(ref) in entries:
        entries[str(ref)]["refs"].append(user.id)
        try:
            await context.bot.send_message(
                ref,
                f"🎉 {user.full_name or 'صاحبك'} دخل المسابقة من لينكك!\n\n{status_text(c, ref)}",
                reply_markup=me_kb(cid),
            )
        except Exception:
            pass
    save_db()
    await send(
        "تم تسجيلك في المسابقة ✅🎉\n\n"
        f"{status_text(c, user.id)}\n\n"
        "ابعت لينكك ده لأصحابك. كل صاحب يدخل منه وهو مشترك في القناة بيتحسب لك:\n"
        f"{link}\n\n"
        f"النتيجة بتتعلن في القناة يوم {fmt_time(c['end_ts'])}",
        disable_web_page_preview=True,
        reply_markup=me_kb(cid),
    )


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args:
        m = re.fullmatch(r"c(\d+)(?:_r(\d+))?", context.args[0])
        if m:
            ref = int(m.group(2)) if m.group(2) else None
            await handle_join(context, user, m.group(1), ref)
            return
    if user.id == OWNER_ID:
        await update.message.reply_text(HELP)
    elif OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {user.id}\nحطه في OWNER_ID وشغل البوت تاني.")
    else:
        await update.message.reply_text("أهلاً! ادخل المسابقة من زرار \"شارك دلوقتي\" في قناة عروض وخصومات 🎁")


# ---------------------------------------------------------------- finishing
async def finish_contest(app, cid):
    c = DB["contests"].get(str(cid))
    if not c or c["status"] != "open":
        return
    c["status"] = "closing"
    save_db()
    bot = app.bot
    need = c.get("min_refs", MIN_REFS)

    cache = {}

    async def member_ok(uid):
        if uid not in cache:
            try:
                cache[uid] = await is_member(bot, uid)
            except Exception:
                cache[uid] = True
            await asyncio.sleep(0.05)
        return cache[uid]

    pool = []  # (uid, entry, valid_invites)
    for uid, e in c["entries"].items():
        if len(e["refs"]) < need:
            continue
        if not await member_ok(int(uid)):
            continue
        valid = 0
        for r in e["refs"]:
            if await member_ok(r):
                valid += 1
        if valid >= need:
            pool.append((int(uid), e, valid))

    chosen = []
    items = pool[:]
    for _ in range(min(c["winners"], len(items))):
        i = random.choices(range(len(items)), weights=[v for _, _, v in items])[0]
        chosen.append(items.pop(i))

    c["status"] = "done"
    c["winner_ids"] = [u for u, _, _ in chosen]
    save_db()

    if c.get("chan_msg"):
        try:
            await bot.edit_message_reply_markup(c["chan_msg"][0], c["chan_msg"][1], reply_markup=None)
        except Exception:
            pass

    if chosen:
        lines = "\n".join(f"🏅 {mention(u, e['name'])} — دعا {v} صديق" for u, e, v in chosen)
        text = (
            "🎉 <b>انتهت المسابقة!</b>\n\n"
            f"🏆 الجايزة: <b>{html.escape(c['prize'])}</b>\n"
            f"👥 عدد المشاركين: {len(c['entries'])}\n"
            f"✅ المؤهلين للسحب: {len(pool)}\n\n"
            f"<b>الفايزين:</b>\n{lines}\n\n"
            "مبروك للفايزين 🎊 هنتواصل معاكم هنا في الخاص.\n\n"
            "📢 قناة عروض وخصومات\n"
            f"{CHANNEL_LINK}"
        )
    else:
        text = (
            "انتهت المسابقة 🙏\n"
            f"مفيش حد استوفى شرط دعوة {need} صديق على الأقل، فمفيش فايز المرة دي.\n\n"
            "تابعوا القناة للمسابقات الجاية."
        )
    try:
        await bot.send_message(CHANNEL, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.warning("announce failed: %s", type(e).__name__)
        if OWNER_ID:
            await bot.send_message(
                OWNER_ID,
                "معرفتش أعلن النتيجة في القناة، تأكد إن البوت أدمن فيها.\n\n" + text,
                parse_mode=ParseMode.HTML,
            )

    for u, _, _ in chosen:
        try:
            await bot.send_message(u, f"🎉 مبروك! كسبت في المسابقة\n🏆 {c['prize']}\nهنتواصل معاك هنا.")
        except Exception:
            pass
    if OWNER_ID:
        names = "\n".join(f"{mention(u, e['name'])} ({u}) — {v} دعوة" for u, e, v in chosen) or "لا يوجد"
        try:
            await bot.send_message(
                OWNER_ID,
                f"خلصت المسابقة رقم {cid} ✅\nالمشاركين: {len(c['entries'])} — المؤهلين: {len(pool)}\nالفايزين:\n{names}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    for cid, c in list(DB["contests"].items()):
        if c["status"] == "open" and c["end_ts"] <= now:
            try:
                await finish_contest(context.application, cid)
            except Exception as e:
                log.warning("finish failed: %s", type(e).__name__)


# ---------------------------------------------------------------- owner
HELP = (
    "أهلاً يا صاحب القناة 👋\n\n"
    "/new — مسابقة جديدة\n"
    "/list — المسابقات المفتوحة\n"
    "/end رقم — إنهاء مسابقة دلوقتي وسحب الفايزين\n"
    "/cancel رقم — إلغاء مسابقة من غير سحب"
)


def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return
        await fn(update, context)

    return wrapper


@owner_only
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new"] = {"step": "prize"}
    await update.message.reply_text("اكتب الجايزة (مثلاً: سماعة بلوتوث). ولو عايز تلغي ابعت: إلغاء")


@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = []
    for cid, c in DB["contests"].items():
        if c["status"] == "open":
            need = c.get("min_refs", MIN_REFS)
            ok = sum(1 for e in c["entries"].values() if len(e["refs"]) >= need)
            rows.append(
                f"#{cid} — {c['prize']}\nالمشاركين: {len(c['entries'])} — المؤهلين: {ok}\nينتهي: {fmt_time(c['end_ts'])}"
            )
    await update.message.reply_text("\n\n".join(rows) or "مفيش مسابقات مفتوحة.")


@owner_only
async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("اكتب كده: /end 1")
        return
    c = DB["contests"].get(context.args[0])
    if not c or c["status"] != "open":
        await update.message.reply_text("المسابقة دي مش مفتوحة.")
        return
    await update.message.reply_text("جاري سحب الفايزين...")
    await finish_contest(context.application, context.args[0])


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("اكتب كده: /cancel 1")
        return
    c = DB["contests"].get(context.args[0])
    if not c or c["status"] != "open":
        await update.message.reply_text("المسابقة دي مش مفتوحة.")
        return
    c["status"] = "cancelled"
    save_db()
    if c.get("chan_msg"):
        try:
            await context.bot.edit_message_reply_markup(c["chan_msg"][0], c["chan_msg"][1], reply_markup=None)
        except Exception:
            pass
    await update.message.reply_text("اتلغت المسابقة.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    st = context.user_data.get("new")
    if not st:
        return
    txt = (update.message.text or "").strip()
    if txt in ("إلغاء", "الغاء"):
        context.user_data.pop("new", None)
        await update.message.reply_text("اتلغى.")
        return

    if st["step"] == "prize":
        st["prize"] = txt
        st["step"] = "winners"
        await update.message.reply_text("كام فايز؟ ابعت رقم (مثلاً 1)")
        return

    if st["step"] == "winners":
        n = parse_number(txt)
        if not n or n < 1 or n > 50:
            await update.message.reply_text("ابعت رقم من 1 لـ 50.")
            return
        st["winners"] = int(n)
        st["step"] = "hours"
        await update.message.reply_text("المسابقة تفضل مفتوحة كام ساعة؟ ابعت رقم (مثلاً 48 يعني يومين)")
        return

    if st["step"] == "hours":
        h = parse_number(txt)
        if not h or h < 0.05 or h > 24 * 60:
            await update.message.reply_text("ابعت عدد ساعات صح (مثلاً 24).")
            return
        st["end_ts"] = time.time() + h * 3600
        st["step"] = "confirm"
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ انشر المسابقة", callback_data="nw:ok"),
                InlineKeyboardButton("❌ إلغاء", callback_data="nw:no"),
            ]]
        )
        await update.message.reply_text(
            "معاينة الإعلان:\n\n" + contest_text(st).replace("<b>", "").replace("</b>", ""),
            reply_markup=kb,
            disable_web_page_preview=True,
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, rest = (q.data or "").partition(":")
    user = q.from_user

    if action == "chk":
        cid, _, ref = rest.partition(":")
        await handle_join(context, user, cid, int(ref) if ref.isdigit() and int(ref) else None)
        return

    if action == "me":
        c = DB["contests"].get(rest)
        if not c or str(user.id) not in c["entries"]:
            await context.bot.send_message(user.id, "إنت مش مسجل في المسابقة دي.")
            return
        await context.bot.send_message(
            user.id,
            status_text(c, user.id) + "\n\nلينكك:\n" + personal_link(context, rest, user.id),
            disable_web_page_preview=True,
        )
        return

    if action == "nw" and user.id == OWNER_ID:
        st = context.user_data.get("new")
        if not st or st.get("step") != "confirm":
            return
        context.user_data.pop("new", None)
        if rest == "no":
            await q.message.reply_text("اتلغى.")
            return
        cid = DB["next"]
        DB["next"] = cid + 1
        c = {
            "prize": st["prize"],
            "winners": st["winners"],
            "min_refs": MIN_REFS,
            "end_ts": st["end_ts"],
            "status": "open",
            "entries": {},
            "chan_msg": None,
        }
        DB["contests"][str(cid)] = c
        try:
            msg = await context.bot.send_message(
                CHANNEL,
                contest_text(c),
                parse_mode=ParseMode.HTML,
                reply_markup=join_kb(context.bot_data["username"], cid),
                disable_web_page_preview=True,
            )
            c["chan_msg"] = [msg.chat_id, msg.message_id]
            save_db()
            await q.message.reply_text(f"اتنشرت المسابقة رقم {cid} في القناة ✅")
        except Exception as e:
            DB["contests"].pop(str(cid), None)
            DB["next"] = cid
            log.warning("post failed: %s", type(e).__name__)
            await q.message.reply_text("معرفتش أنشر في القناة. تأكد إن البوت أدمن في قناة عروض وخصومات.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


async def post_init(app):
    me = await app.bot.get_me()
    app.bot_data["username"] = me.username


def main():
    if not TOKEN or not OWNER_ID:
        print("لازم تعمل export لـ CONTEST_BOT_TOKEN و OWNER_ID الأول.")
        return
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    if app.job_queue is None:
        print("job-queue مش متثبتة. نفّذ: pip install 'python-telegram-bot[job-queue]'")
        return
    app.job_queue.run_repeating(check_job, interval=30, first=10)
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("Contest bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
