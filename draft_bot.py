import os
import re
import json
import html
import random
import string
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
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
log = logging.getLogger("draft_bot")

TOKEN = os.environ.get("DRAFT_BOT_TOKEN", "")
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

CHANNEL = "@PromoCodeOnlain"
BASE = os.path.dirname(os.path.abspath(__file__))
DRAFTS = os.path.join(BASE, "drafts")
PID_RE = re.compile(r"[a-z0-9]{8}")


# ---------------------------------------------------------------- draft store
def new_pid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _dpath(pid, ext):
    return os.path.join(DRAFTS, f"{pid}.{ext}")


def store_draft(pid, image, post, src=None):
    os.makedirs(DRAFTS, exist_ok=True)
    with open(_dpath(pid, "json"), "w", encoding="utf-8") as f:
        json.dump({"post": post, "image": bool(image), "src": src or []}, f, ensure_ascii=False)
    if image:
        with open(_dpath(pid, "jpg"), "wb") as f:
            f.write(image)


def load_draft(pid):
    if not PID_RE.fullmatch(pid or ""):
        return None
    try:
        with open(_dpath(pid, "json"), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    img = None
    if d.get("image"):
        try:
            with open(_dpath(pid, "jpg"), "rb") as f:
                img = f.read()
        except Exception:
            img = None
    d["image"] = img
    return d


def set_src(pid, src):
    d = load_draft(pid)
    if d:
        store_draft(pid, d["image"], d["post"], src)


def drop_draft(pid):
    if not PID_RE.fullmatch(pid or ""):
        return
    for ext in ("json", "jpg"):
        try:
            os.remove(_dpath(pid, ext))
        except OSError:
            pass


def plain(post):
    return html.unescape(re.sub(r"<[^>]+>", "", post))


def make_kb(pid):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ نشر", callback_data=f"pub:{pid}"),
                InlineKeyboardButton("✏️ تعديل", callback_data=f"ed:{pid}"),
            ],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"cx:{pid}")],
        ]
    )


def is_photo_caption(image, post):
    return bool(image) and len(post) <= 1000


async def send_post(bot, chat_id, image, post):
    if is_photo_caption(image, post):
        await bot.send_photo(chat_id, photo=image, caption=post, parse_mode=ParseMode.HTML)
        return
    if image:
        await bot.send_photo(chat_id, photo=image)
    await bot.send_message(chat_id, post, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def clear_src(bot, item):
    for chat, mid in item.get("src") or []:
        try:
            await bot.delete_message(chat, mid)
        except Exception:
            pass


# ---------------------------------------------------------------- handlers
BUSY = set()  # مسودات بتتنشر دلوقتي (عشان الضغط المتكرر ميعملش بوست مكرر)


async def safe_answer(q, text=None, alert=False):
    """رد على الزرار من غير ما يوقّف الباقي لو الضغطة قديمة أو النت بطيء."""
    try:
        await q.answer(text, show_alert=alert)
    except Exception as e:
        log.warning("answer skipped: %s", type(e).__name__)


async def publish_with_retry(bot, image, post):
    """بينشر مرة واحدة. لو تليجرام قال استنى (RetryAfter) بيستنى ويعيد. على المهلة (TimedOut) مبيعيدش عشان ميتكررش البوست."""
    try:
        await send_post(bot, CHANNEL, image, post)
    except RetryAfter as e:
        await asyncio.sleep(min(int(getattr(e, "retry_after", 5)) + 1, 60))
        await send_post(bot, CHANNEL, image, post)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {uid}\nحطه في OWNER_ID وشغل البوت تاني.")
    elif uid == OWNER_ID:
        await update.message.reply_text("جاهز ✅ المسودات هتظهر في قناة مسودة العروض بأزرار نشر وتعديل.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, _, val = (q.data or "").partition(":")
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "مش مسموحلك تستخدم الزرار ده.", True)
        return
    if action == "pub":
        await safe_answer(q, "⏳ جاري النشر...")
    elif action != "ed":
        await safe_answer(q)

    async def say(text, **kw):
        try:
            await context.bot.send_message(uid, text, **kw)
        except Exception as e:
            log.warning("say failed: %s", type(e).__name__)

    item = load_draft(val)
    if not item:
        await say("البوست ده انتهى.")
        if action == "ed":
            await safe_answer(q)
        return

    if action == "cx":
        await clear_src(context.bot, item)
        drop_draft(val)
        return

    if action == "ed":
        context.bot_data.setdefault("editing", {})[uid] = val
        try:
            await context.bot.send_message(uid, plain(item["post"]), disable_web_page_preview=True)
            await context.bot.send_message(
                uid,
                "✏️ انسخ النص اللي فوق، عدّل عليه، وابعته هنا.\nعشان تلغي التعديل ابعت: إلغاء",
            )
            await safe_answer(q)
        except Exception:
            await safe_answer(q, "افتح البوت وابعتله /start الأول، وبعدين دوس تعديل تاني.", True)
        return

    if action == "pub":
        if val in BUSY:
            await say("⏳ البوست ده بيتنشر دلوقتي، استنى ثواني.")
            return
        BUSY.add(val)
        try:
            await publish_with_retry(context.bot, item["image"], item["post"])
        except TimedOut:
            log.warning("publish timed out")
            await say(
                "⚠️ النت بطيء والرد اتأخر، وممكن البوست يكون اتنشر فعلاً.\n"
                "راجع قناة عروض وخصومات الأول، ولو مش موجود دوس ✅ نشر تاني (المسودة لسه موجودة)."
            )
            return
        except Exception as e:
            log.exception("publish failed")
            await say(
                f"معرفتش أنشر: {type(e).__name__}\n"
                "المسودة لسه موجودة، دوس ✅ نشر تاني. ولو اتكرر تأكد إن البوت أدمن في قناة عروض وخصومات."
            )
            return
        finally:
            BUSY.discard(val)
        await clear_src(context.bot, item)
        drop_draft(val)
        await say("اتنشر في قناة عروض وخصومات ✅")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    editing = context.bot_data.setdefault("editing", {})
    if uid not in editing:
        await update.message.reply_text("التعديل بيتم من زرار ✏️ تعديل اللي تحت كل مسودة.")
        return
    pid = editing.pop(uid)
    txt = (update.message.text or "").strip()
    item = load_draft(pid)
    if not item:
        await update.message.reply_text("البوست ده انتهى.")
        return
    if txt in ("إلغاء", "الغاء"):
        await update.message.reply_text("اتلغى التعديل.")
        return

    post = html.escape(txt)
    old_post, image, src = item["post"], item["image"], item.get("src") or []
    store_draft(pid, image, post, src)

    # عدّل الرسالة في قناة المسودات مكانها لو نوعها ما اتغيرش
    if src and is_photo_caption(image, old_post) == is_photo_caption(image, post):
        chat, mid = src[-1]
        try:
            if is_photo_caption(image, post):
                await context.bot.edit_message_caption(
                    chat, mid, caption=post, parse_mode=ParseMode.HTML, reply_markup=make_kb(pid)
                )
            else:
                await context.bot.edit_message_text(
                    post, chat, mid, parse_mode=ParseMode.HTML,
                    reply_markup=make_kb(pid), disable_web_page_preview=True,
                )
            await update.message.reply_text("اتعدل في قناة المسودات ✅")
            return
        except Exception as e:
            log.warning("in-place edit failed: %s", type(e).__name__)

    # غير كده ابعت المعاينة هنا بأزرارها
    kb = make_kb(pid)
    if is_photo_caption(image, post):
        await context.bot.send_photo(uid, photo=image, caption=post, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        if image:
            await context.bot.send_photo(uid, photo=image)
        await context.bot.send_message(
            uid, post, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
        )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


def build_app():
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)          # ضغطة بطيئة متعطلش الباقي
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)                 # رفع الصورة على نت ضعيف
        .pool_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(40)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def start_background():
    """بيشغّل استقبال ضغطات الأزرار جوه نفس برنامج forwarder (من غير ما تشغّل draft_bot لوحده)."""
    import threading

    def run():
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            app = build_app()
            log.info("Draft buttons are listening (publish / edit / cancel)")
            app.run_polling(stop_signals=None, close_loop=False)
        except Exception as e:
            log.error("draft buttons stopped: %s %s", type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main():
    if not TOKEN or not OWNER_ID:
        print("لازم تعمل export لـ DRAFT_BOT_TOKEN و OWNER_ID الأول.")
        return
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = build_app()
    print("Draft bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
