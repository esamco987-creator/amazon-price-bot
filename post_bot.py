import os
import re
import io
import json
import uuid
import html
import random
import asyncio
import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from telegram import (
    Update, LinkPreviewOptions, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["POST_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
CHANNEL = "@PromoCodeOnlain"
CHANNEL_URL = "https://t.me/PromoCodeOnlain"
MY_TAG = "mixdeals-21"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(BASE_DIR, "logo.png")
USERS_FILE = os.path.join(BASE_DIR, "allowed_users.json")

CANVAS = 1080        # مقاس الصورة النهائية (مربع)
PRODUCT_BOX = 0.80   # حجم المنتج جوه المربع
LOGO_RATIO = 0.16    # حجم الشعار نسبة لعرض المربع

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}
URL_RE = re.compile(r"https?://[^\s]+")
AMAZON_RE = re.compile(r"amazon\.|amzn\.|link\.amazon")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")

OPENERS = [
    "🔥 عرض مميز",
    "⚡ فرصة للتوفير",
    "💥 خصم يستاهل",
    "🛍️ اكتشف العرض ده",
    "🎯 عرض النهاردة",
]
CLOSERS = [
    "الأسعار بتتغير بسرعة، اتأكد من السعر في الصفحة قبل الشراء.",
    "السعر المكتوب وقت النشر وممكن يتغير في أي لحظة.",
]


def load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def save_users(users):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in users.items()}, f, ensure_ascii=False)
    except Exception as e:
        logging.warning("Could not save users: %s", e)


ALLOWED = load_users()  # رقم الحساب -> الاسم
asked = set()  # اللي بعتوا طلب دخول في الجلسة دي


def is_allowed(uid):
    return bool(uid) and (uid == OWNER_ID or uid in ALLOWED)


def parse_price(text):
    text = (text or "").translate(AR_DIGITS).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def fmt(n):
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,.2f}"


def resolve(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        return r.url, r.text
    except Exception as e:
        logging.warning("Fetch failed: %s", e)
        return url, ""


def affiliate(final):
    if not AMAZON_RE.search(final):
        return final
    host = urlparse(final).netloc or "www.amazon.eg"
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", final)
    if m:
        return f"https://{host}/dp/{m.group(1)}?tag={MY_TAG}"
    if "tag=" in final:
        return re.sub(r"([?&])tag=[^&]*", r"\1tag=" + MY_TAG, final)
    sep = "&" if "?" in final else "?"
    return f"{final}{sep}tag={MY_TAG}"


def first_text(soup, selectors):
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            t = tag.get_text(" ", strip=True)
            if t:
                return t
    return None


def read_product(page):
    soup = BeautifulSoup(page, "html.parser")
    title = first_text(soup, ["#productTitle"])
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()

    price = parse_price(first_text(soup, [
        "#corePrice_feature_div span.a-offscreen",
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ]))
    old = parse_price(first_text(soup, [
        "span.a-price.a-text-price span.a-offscreen",
        ".basisPrice span.a-offscreen",
        "#listPrice",
    ]))

    rating = None
    rt = first_text(soup, ["#acrPopover .a-icon-alt", "span.a-icon-alt"])
    if rt:
        m = re.search(r"\d+(?:[.,]\d+)?", rt.translate(AR_DIGITS))
        if m:
            val = float(m.group().replace(",", "."))
            if 0 < val <= 5:
                rating = val

    # أكبر نسخة من الصورة
    img_url = None
    img = soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
    if img:
        dyn = img.get("data-a-dynamic-image")
        if dyn:
            try:
                sizes = json.loads(html.unescape(dyn))
                img_url = max(sizes, key=lambda u: sizes[u][0] * sizes[u][1])
            except Exception:
                img_url = None
        if not img_url:
            img_url = img.get("data-old-hires") or img.get("src")
    if not img_url:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img_url = og["content"]
    return title, price, old, rating, img_url


def full_size(url):
    """بيشيل علامة التصغير من لينك صور أمازون عشان ناخد الصورة الأصلية."""
    return re.sub(r"\._[^/]*_\.(jpg|jpeg|png|webp)$", r".\1", url, flags=re.I)


def prepare_image(data):
    """مربع 1080x1080 بخلفية بيضا، المنتج في النص، والشعار في الركن."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        white.alpha_composite(img)
        img = white.convert("RGB")

        box = int(CANVAS * PRODUCT_BOX)
        scale = min(box / img.width, box / img.height, 2.0)
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(size, Image.LANCZOS)

        canvas = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
        x = (CANVAS - size[0]) // 2
        y = int((CANVAS - size[1]) / 2 - CANVAS * 0.02)
        canvas.paste(img, (x, max(y, 0)))

        logo = Image.open(LOGO_PATH).convert("RGBA")
        w = int(CANVAS * LOGO_RATIO)
        h = int(logo.height * w / logo.width)
        logo = logo.resize((w, h), Image.LANCZOS)
        margin = int(CANVAS * 0.02)
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(logo, (CANVAS - w - margin, CANVAS - h - margin))

        out = io.BytesIO()
        canvas.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()
    except Exception as e:
        logging.warning("Image preparation failed: %s", e)
        return data


def fetch_bytes(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and len(r.content) >= 2000:
            return r.content
    except Exception as e:
        logging.warning("Image download failed: %s", e)
    return None


def download_image(img_url):
    """بيرجع bytes للصورة بعد التظبيط (وعليها الشعار) أو None."""
    if not img_url or not img_url.startswith("http"):
        return None
    data = None
    if "amazon" in img_url:
        data = fetch_bytes(full_size(img_url))
    if data is None:
        data = fetch_bytes(img_url)
    if data is None:
        return None
    return prepare_image(data)


def short_title(t, n=90):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= n:
        return t
    return t[:n].rsplit(" ", 1)[0] + "…"


def build_post(title, price, old, rating, link):
    esc = html.escape
    parts = [random.choice(OPENERS) + "\n<b>" + esc(short_title(title)) + "</b>"]
    info = []
    if price:
        info.append("💰 السعر: " + fmt(price) + " جنيه")
        if old and old > price:
            pct = round((old - price) / old * 100)
            info.append("🏷️ كان: " + fmt(old) + " جنيه (خصم " + str(pct) + "%)")
    if rating:
        info.append("⭐ التقييم: " + f"{rating:.1f}" + " من 5")
    if info:
        parts.append("\n".join(info))
    parts.append("🛒 اشتري من هنا:\n" + esc(link))
    parts.append(esc(random.choice(CLOSERS)))
    parts.append("📢 قناة عروض وخصومات\nللمزيد من العروض يوميًا:\n" + CHANNEL_URL)
    return "\n\n".join(parts)


def photo_file(data):
    bio = io.BytesIO(data)
    bio.name = "product.jpg"
    return bio


async def send_post(bot, chat_id, image, post, markup=None):
    """بيبعت البوست (بالصورة لو موجودة). الزرارين بيتحطوا على آخر رسالة."""
    no_preview = LinkPreviewOptions(is_disabled=True)
    if image and len(post) <= 1000:
        await bot.send_photo(
            chat_id, photo=photo_file(image), caption=post,
            parse_mode="HTML", reply_markup=markup,
        )
        return
    if image:
        await bot.send_photo(chat_id, photo=photo_file(image))
    await bot.send_message(
        chat_id, post, parse_mode="HTML",
        link_preview_options=no_preview, reply_markup=markup,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    if not OWNER_ID:
        await update.message.reply_text(
            f"رقم حسابك هو: {uid}\nاحفظه، هنحتاجه عشان البوت يخدمك انت بس."
        )
        return
    if is_allowed(uid):
        await update.message.reply_text(
            "ابعتلي لينك أي منتج، وهبعتلك معاينة البوست وتحتها زرار نشر."
        )
        return

    await update.message.reply_text(
        "البوت ده خاص. بعتّ طلب للمسؤول، وهتوصلك رسالة أول ما يوافق."
    )
    if uid in asked:
        return
    asked.add(uid)
    name = user.full_name or str(uid)
    if user.username:
        name += f" (@{user.username})"
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ قبول", callback_data=f"approve:{uid}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"deny:{uid}"),
    ]])
    try:
        await context.bot.send_message(
            OWNER_ID, f"طلب دخول جديد:\n{name}\nرقمه: {uid}", reply_markup=markup
        )
    except Exception as e:
        logging.warning("Could not notify owner: %s", e)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ALLOWED:
        await update.message.reply_text("مفيش حد مضاف غيرك.")
        return
    lines = [f"{uid} - {name}" for uid, name in ALLOWED.items()]
    await update.message.reply_text(
        "المضافين:\n" + "\n".join(lines) + "\n\nلحذف حد: /remove الرقم"
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        target = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("اكتب كده: /remove الرقم")
        return
    if ALLOWED.pop(target, None) is None:
        await update.message.reply_text("الرقم ده مش في القايمة.")
        return
    save_users(ALLOWED)
    asked.discard(target)
    await update.message.reply_text("اتحذف.")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not OWNER_ID:
        await update.message.reply_text(f"رقم حسابك هو: {uid}")
        return
    if not is_allowed(uid):
        await update.message.reply_text("البوت ده خاص. ابعت /start عشان تطلب الدخول.")
        return

    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("ابعتلي لينك المنتج.")
        return
    url = m.group().rstrip(").,")

    await update.message.reply_text("ثواني، بجهز البوست...")
    final, page = await asyncio.to_thread(resolve, url)
    title, price, old, rating, img_url = read_product(page)
    if not title:
        await update.message.reply_text(
            "معرفتش أقرا بيانات المنتج. جرّب لينك تاني أو حاول بعد شوية."
        )
        return

    link = affiliate(final)
    image = await asyncio.to_thread(download_image, img_url)
    post = build_post(title, price, old, rating, link)

    pid = uuid.uuid4().hex[:8]
    context.bot_data.setdefault("pending", {})[pid] = {
        "uid": uid, "image": image, "post": post,
    }
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نشر", callback_data="pub:" + pid),
        InlineKeyboardButton("❌ إلغاء", callback_data="cancel:" + pid),
    ]])

    try:
        await send_post(context.bot, uid, image, post, markup)
    except Exception as e:
        logging.warning("Preview failed: %s", e)
        context.bot_data["pending"].pop(pid, None)
        await update.message.reply_text(f"معرفتش أجهز المعاينة: {e}")
        return

    notes = []
    if not price:
        notes.append("مقدرتش أقرا السعر فالبوست من غير سعر.")
    if not image:
        notes.append("مقدرتش أجيب الصورة فالبوست من غير صورة.")
    if notes:
        await update.message.reply_text(" ".join(notes))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    action, _, arg = (q.data or "").partition(":")

    # قبول أو رفض طلب دخول (للمسؤول بس)
    if action in ("approve", "deny"):
        if uid != OWNER_ID:
            await q.answer()
            return
        target = int(arg)
        await q.edit_message_reply_markup(reply_markup=None)
        if action == "approve":
            lines = (q.message.text or "").splitlines()
            ALLOWED[target] = lines[1] if len(lines) > 1 else str(target)
            save_users(ALLOWED)
            await q.answer("تمت الإضافة")
            await q.message.reply_text("✅ تمت إضافته.")
            try:
                await context.bot.send_message(
                    target, "تم قبولك. ابعتلي لينك أي منتج وأنا أجهز البوست."
                )
            except Exception as e:
                logging.warning("Could not notify user: %s", e)
        else:
            await q.answer("اترفض")
            await q.message.reply_text("❌ اترفض.")
        return

    # نشر أو إلغاء بوست
    if not is_allowed(uid):
        await q.answer()
        return
    pending = context.bot_data.setdefault("pending", {})
    item = pending.get(arg)
    if not item or item["uid"] != uid:
        await q.answer("المعاينة دي خلصت. ابعت اللينك تاني.", show_alert=True)
        return
    pending.pop(arg)

    if action == "cancel":
        await q.answer("اتلغى")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("❌ اتلغى، ومنزلش في القناة.")
        return

    try:
        await send_post(context.bot, CHANNEL, item["image"], item["post"])
    except Exception as e:
        logging.warning("Post failed: %s", e)
        pending[arg] = item
        await q.answer(f"فشل النشر: {e}", show_alert=True)
        return

    await q.answer("تم النشر")
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text("✅ تم النشر في القناة.")


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()


if __name__ == "__main__":
    main()
