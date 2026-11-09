"""
بوت خاص: أسعار العملات الرقمية + متابعة P2P بايننس (تختار العملة وطريقة الدفع وشراء/بيع) + تنبيهات.
الإعدادات: CRYPTO_BOT_TOKEN (من BotFather) و OWNER_ID (رقمك).
بيستخدم البيانات العامة بس: مفيش مفاتيح بايننس ولا أي صلاحية على حسابك.
"""
import os
import re
import json
import time
import asyncio
import logging

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
log = logging.getLogger("crypto_bot")

TOKEN = os.environ.get("CRYPTO_BOT_TOKEN", "")
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

CHECK_EVERY = int(os.environ.get("CRYPTO_CHECK_EVERY", "90"))  # كل قد إيه يفحص التنبيهات (ثواني)
FIAT = os.environ.get("P2P_FIAT", "EGP")
P2P_ASSETS = ["USDT", "USDC", "BTC", "ETH", "BNB", "FDUSD"]
MAX_ALERTS = 30
METHODS_TTL = 3600

SPOT_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]
P2P_SEARCH = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
P2P_FILTERS = "https://p2p.binance.com/bapi/c2c/v2/public/c2c/adv/filter-conditions"
UA = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Content-Type": "application/json",
}

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "crypto_alerts.json")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")

ABOVE = {"above", "over", ">", "فوق", "اعلى", "أعلى", "اكبر", "أكبر"}
BELOW = {"below", "under", "<", "تحت", "اقل", "أقل", "اصغر", "أصغر"}


# ---------------------------------------------------------------- storage
def migrate(items):
    """تنبيهات النسخة القديمة (key) تتحول للشكل الجديد."""
    out = []
    for a in items:
        if "kind" in a:
            out.append(a)
            continue
        key = a.get("key", "")
        if key in ("P2P_BUY", "P2P_SELL"):
            out.append({"kind": "p2p", "asset": "USDT", "side": key[4:], "method": None,
                        "mname": None, "op": a["op"], "value": a["value"]})
        elif key:
            out.append({"kind": "spot", "coin": key, "op": a["op"], "value": a["value"]})
    return out


def load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return migrate(d) if isinstance(d, list) else []
    except Exception:
        return []


def save():
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alerts, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log.warning("save failed: %s", type(e).__name__)


alerts = load()


# ---------------------------------------------------------------- binance: spot
def spot_price(coin):
    """سعر العملة مقابل USDT. يرمي ValueError لو العملة مش موجودة."""
    sym = coin.upper() + "USDT"
    last = None
    for b in SPOT_BASES:
        try:
            r = requests.get(f"{b}/api/v3/ticker/price", params={"symbol": sym}, headers=UA, timeout=20)
            if r.status_code == 200:
                return float(r.json()["price"])
            if r.status_code == 400:
                raise ValueError("unknown")
            last = f"status {r.status_code}"
        except ValueError:
            raise
        except Exception as e:
            last = type(e).__name__
    raise RuntimeError(last or "failed")


# ---------------------------------------------------------------- binance: p2p
def parse_ads(data, top=5):
    """يرجّع (قايمة إعلانات مختصرة، best، avg) أو None لو مفيش إعلانات."""
    rows = (data or {}).get("data") or []
    ads = []
    for r in rows:
        try:
            adv = r["adv"]
            ads.append(
                {
                    "price": float(adv["price"]),
                    "name": ((r.get("advertiser") or {}).get("nickName")) or "-",
                    "qty": float(adv.get("tradableQuantity") or 0),
                    "min": float(adv.get("minSingleTransAmount") or 0),
                    "max": float(adv.get("maxSingleTransAmount") or 0),
                    "methods": [m.get("tradeMethodName") or m.get("identifier") for m in adv.get("tradeMethods") or []],
                    "mids": [(m.get("identifier"), m.get("tradeMethodName") or m.get("identifier")) for m in adv.get("tradeMethods") or []],
                }
            )
        except Exception:
            continue
    if not ads:
        return None
    first = ads[:top]
    return ads, ads[0]["price"], sum(a["price"] for a in first) / len(first)


def p2p_search(side, asset, method=None, rows=10, page=1):
    body = {
        "fiat": FIAT,
        "page": page,
        "rows": rows,
        "tradeType": side,
        "asset": asset,
        "countries": [],
        "proMerchantAds": False,
        "shieldMerchantAds": False,
        "publisherType": None,
        "payTypes": [method] if method else [],
    }
    r = requests.post(P2P_SEARCH, json=body, headers=UA, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"status {r.status_code}")
    return r.json()


def p2p_price(side, asset="USDT", method=None):
    res = parse_ads(p2p_search(side, asset, method))
    if not res:
        raise RuntimeError("مفيش إعلانات")
    return res


def fetch_methods():
    """كل طرق الدفع المتاحة للعملة المحلية على P2P. يرجع [(identifier, name)]."""
    found = {}
    try:
        r = requests.post(P2P_FILTERS, json={"fiat": FIAT}, headers=UA, timeout=25)
        if r.status_code == 200:
            for m in ((r.json().get("data") or {}).get("tradeMethods")) or []:
                ident = m.get("identifier")
                if ident:
                    found[ident] = m.get("tradeMethodName") or ident
    except Exception as e:
        log.warning("filter-conditions failed: %s", type(e).__name__)
    if not found:
        # خطة بديلة: نجمع الطرق من الإعلانات الحالية
        for side in ("BUY", "SELL"):
            for page in (1, 2, 3):
                try:
                    res = parse_ads(p2p_search(side, "USDT", None, rows=20, page=page))
                except Exception:
                    break
                if not res:
                    break
                for ad in res[0]:
                    for ident, name in ad["mids"]:
                        if ident:
                            found[ident] = name
    return sorted(found.items(), key=lambda x: x[1])


def get_value(al):
    if al["kind"] == "spot":
        return spot_price(al["coin"])
    return p2p_price(al["side"], al["asset"], al.get("method"))[1]


# ---------------------------------------------------------------- helpers
def num(text):
    m = re.search(r"\d[\d,]*\.?\d*", (text or "").translate(AR_DIGITS))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def money(v):
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def side_word(side):
    return "شراء" if side == "BUY" else "بيع"


def p2p_title(asset, side, mname):
    return f"{side_word(side)} {asset} بالـ{FIAT} — {mname or 'كل طرق الدفع'}"


def alert_title(al):
    if al["kind"] == "spot":
        return al["coin"]
    return p2p_title(al["asset"], al["side"], al.get("mname"))


def alert_unit(al):
    return "USDT" if al["kind"] == "spot" else FIAT


def norm_coin(text):
    t = (text or "").strip().upper()
    return t if re.fullmatch(r"[A-Z0-9]{2,10}", t) else None


def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not OWNER_ID:
            if update.message:
                await update.message.reply_text(f"رقمك (ID): {uid}\nحطه في OWNER_ID وشغل البوت تاني.")
            return
        if uid != OWNER_ID:
            return
        await fn(update, context)

    return wrapper


HELP = (
    "أهلاً! بوت العملات الرقمية 📈\n\n"
    "اكتب اسم عملة وهرد بسعرها، مثال: BTC\n\n"
    "/p2p — تابع P2P بايننس: تختار شراء أو بيع، والعملة، وطريقة الدفع\n"
    "/alert BTC فوق 70000 — نبهني لما سعر عملة يعدّي رقم\n"
    "/alerts — التنبيهات الشغالة\n"
    "/remove رقم — احذف تنبيه\n\n"
    "تنبيه P2P بيتعمل من /p2p: اختار اللي تتابعه وبعدين دوس 🔔 نبهني."
)


# ---------------------------------------------------------------- commands
@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def reply_price(update, coin):
    try:
        v = await asyncio.to_thread(spot_price, coin)
        await update.message.reply_text(f"{coin}: {money(v)} USDT")
    except ValueError:
        await update.message.reply_text("العملة دي مش موجودة في بايننس (جرب BTC أو ETH أو SOL).")
    except Exception as e:
        await update.message.reply_text(f"معرفتش أجيب السعر دلوقتي ({e}). جرب تاني بعد شوية.")


@owner_only
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = norm_coin(context.args[0]) if context.args else None
    if not c:
        await update.message.reply_text("اكتب اسم العملة، مثال: /price BTC")
        return
    await reply_price(update, c)


# ---- P2P: قايمة اختيارات بأزرار
def kb(rows):
    return InlineKeyboardMarkup(rows)


def sel(context):
    return context.user_data.setdefault("p2p", {})


@owner_only
async def cmd_p2p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["p2p"] = {}
    await update.message.reply_text(
        f"💱 متابعة P2P بايننس ({FIAT})\n\nانت عايز:",
        reply_markup=kb([[
            InlineKeyboardButton("🟢 أشتري", callback_data="p2:side:BUY"),
            InlineKeyboardButton("🔴 أبيع", callback_data="p2:side:SELL"),
        ]]),
    )


def asset_kb():
    rows, row = [], []
    for a in P2P_ASSETS:
        row.append(InlineKeyboardButton(a, callback_data=f"p2:ast:{a}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(rows)


async def get_methods(context):
    cache = context.bot_data.get("methods")
    if cache and time.time() - cache["ts"] < METHODS_TTL and cache["list"]:
        return cache["list"]
    lst = await asyncio.to_thread(fetch_methods)
    if lst:
        context.bot_data["methods"] = {"ts": time.time(), "list": lst}
    return lst


def methods_kb(lst):
    rows = [[InlineKeyboardButton("✅ كل طرق الدفع", callback_data="p2:mt:all")]]
    row = []
    for i, (_, name) in enumerate(lst[:60]):
        row.append(InlineKeyboardButton(name[:22], callback_data=f"p2:mt:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ من الأول", callback_data="p2:back")])
    return kb(rows)


async def show_result(q, context, refresh=False):
    s = sel(context)
    title = p2p_title(s["asset"], s["side"], s.get("mname"))
    try:
        ads, best, avg = await asyncio.to_thread(p2p_price, s["side"], s["asset"], s.get("method"))
    except Exception as e:
        await q.edit_message_text(
            f"{title}\n\nمعرفتش أجيب الأسعار دلوقتي ({e}).",
            reply_markup=kb([[InlineKeyboardButton("🔄 جرب تاني", callback_data="p2:ref"),
                              InlineKeyboardButton("⬅️ من الأول", callback_data="p2:back")]]),
        )
        return
    lines = [f"💱 {title}", "", f"أحسن سعر: {money(best)} {FIAT}", f"متوسط أول 5: {money(avg)} {FIAT}", "", "أفضل العروض:"]
    for i, a in enumerate(ads[:5], 1):
        lines.append(
            f"{i}. {money(a['price'])} — {a['name']}\n"
            f"   متاح {money(a['qty'])} {s['asset']} | الحد {money(a['min'])}–{money(a['max'])} {FIAT}\n"
            f"   {' / '.join(m for m in a['methods'] if m)}"
        )
    lines += ["", time.strftime("آخر تحديث: %H:%M:%S")]
    try:
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=kb([
                [InlineKeyboardButton("🔔 نبهني", callback_data="p2:al"),
                 InlineKeyboardButton("🔄 تحديث", callback_data="p2:ref")],
                [InlineKeyboardButton("⬅️ من الأول", callback_data="p2:back")],
            ]),
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", type(e).__name__)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not OWNER_ID or q.from_user.id != OWNER_ID:
        await q.answer()
        return
    parts = (q.data or "").split(":")
    if parts[0] != "p2" or len(parts) < 2:
        await q.answer()
        return
    act = parts[1]
    val = parts[2] if len(parts) > 2 else ""
    s = sel(context)
    await q.answer()

    if act == "back":
        context.user_data["p2p"] = {}
        await q.edit_message_text(
            f"💱 متابعة P2P بايننس ({FIAT})\n\nانت عايز:",
            reply_markup=kb([[
                InlineKeyboardButton("🟢 أشتري", callback_data="p2:side:BUY"),
                InlineKeyboardButton("🔴 أبيع", callback_data="p2:side:SELL"),
            ]]),
        )
    elif act == "side" and val in ("BUY", "SELL"):
        s["side"] = val
        await q.edit_message_text(f"{side_word(val)} — اختار العملة:", reply_markup=asset_kb())
    elif act == "ast" and val in P2P_ASSETS and s.get("side"):
        s["asset"] = val
        await q.edit_message_text("⏳ بجيب طرق الدفع المتاحة...")
        lst = await get_methods(context)
        if not lst:
            s["method"], s["mname"] = None, None
            await q.edit_message_text("معرفتش أجيب قايمة طرق الدفع، هعرض كل الطرق.")
            await show_result(q, context)
            return
        await q.edit_message_text(
            f"{side_word(s['side'])} {val} — اختار طريقة الدفع:", reply_markup=methods_kb(lst)
        )
    elif act == "mt" and s.get("side") and s.get("asset"):
        if val == "all":
            s["method"], s["mname"] = None, None
        else:
            lst = await get_methods(context)
            try:
                s["method"], s["mname"] = lst[int(val)]
            except Exception:
                await q.edit_message_text("القايمة اتحدّثت، ابدأ من الأول: /p2p")
                return
        await show_result(q, context)
    elif act == "ref" and s.get("side") and s.get("asset"):
        await show_result(q, context, refresh=True)
    elif act == "al" and s.get("side") and s.get("asset"):
        context.user_data["await_alert"] = dict(s)
        want = "ينزل لـ" if s["side"] == "BUY" else "يوصل لـ"
        await context.bot.send_message(
            OWNER_ID,
            f"🔔 {p2p_title(s['asset'], s['side'], s.get('mname'))}\n"
            f"ابعتلي السعر (بالجنيه) اللي تحب نبهك لما {want} ده. مثال: 50.5\n"
            f"للإلغاء ابعت: إلغاء",
        )
    else:
        await q.edit_message_text("ابدأ من الأول: /p2p")


@owner_only
async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) < 3:
        await update.message.reply_text("الصيغة: /alert BTC فوق 70000\nتنبيهات P2P من /p2p ← 🔔 نبهني")
        return
    coin = norm_coin(a[0])
    word = a[1].lower()
    op = "above" if word in ABOVE else "below" if word in BELOW else None
    value = num(a[2])
    if not coin or not op or value is None:
        await update.message.reply_text("مفهمتش. مثال: /alert BTC فوق 70000 (فوق أو تحت)")
        return
    if len(alerts) >= MAX_ALERTS:
        await update.message.reply_text(f"وصلت للحد الأقصى ({MAX_ALERTS}). احذف تنبيه بـ /remove.")
        return
    al = {"kind": "spot", "coin": coin, "op": op, "value": value}
    try:
        cur = await asyncio.to_thread(get_value, al)
    except ValueError:
        await update.message.reply_text("العملة دي مش موجودة في بايننس.")
        return
    except Exception as e:
        await update.message.reply_text(f"معرفتش أجيب السعر الحالي ({e}). جرب تاني.")
        return
    alerts.append(al)
    save()
    w = "يعدّي" if op == "above" else "ينزل عن"
    await update.message.reply_text(
        f"تمام ✅ هنبهك لما {coin} {w} {money(value)} USDT.\nالسعر دلوقتي: {money(cur)}"
    )


@owner_only
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not alerts:
        await update.message.reply_text("مفيش تنبيهات شغالة.")
        return
    lines = []
    for i, al in enumerate(alerts, 1):
        w = "فوق" if al["op"] == "above" else "تحت"
        lines.append(f"{i}. {alert_title(al)} {w} {money(al['value'])} {alert_unit(al)}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0].translate(AR_DIGITS)) - 1
        if n < 0:
            raise IndexError
        removed = alerts.pop(n)
        save()
        await update.message.reply_text(f"اتحذف: {alert_title(removed)}")
    except (IndexError, ValueError):
        await update.message.reply_text("اكتب رقم صح من /alerts، مثال: /remove 1")


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    pending = context.user_data.get("await_alert")
    if pending:
        context.user_data.pop("await_alert", None)
        if text in ("إلغاء", "الغاء"):
            await update.message.reply_text("اتلغى.")
            return
        value = num(text)
        if value is None:
            await update.message.reply_text("مفهمتش الرقم. افتح /p2p وجرب تاني.")
            return
        if len(alerts) >= MAX_ALERTS:
            await update.message.reply_text(f"وصلت للحد الأقصى ({MAX_ALERTS}). احذف تنبيه بـ /remove.")
            return
        op = "below" if pending["side"] == "BUY" else "above"
        alerts.append({
            "kind": "p2p", "asset": pending["asset"], "side": pending["side"],
            "method": pending.get("method"), "mname": pending.get("mname"),
            "op": op, "value": value,
        })
        save()
        w = "ينزل لـ" if op == "below" else "يوصل لـ"
        await update.message.reply_text(
            f"تمام ✅ هنبهك لما {p2p_title(pending['asset'], pending['side'], pending.get('mname'))} {w} {money(value)} {FIAT} أو أحسن."
        )
        return
    c = norm_coin(text)
    if c:
        await reply_price(update, c)
    else:
        await update.message.reply_text("اكتب اسم عملة (مثال: BTC) أو /p2p أو /start للمساعدة.")


# ---------------------------------------------------------------- alerts job
def is_hit(al, cur):
    return cur >= al["value"] if al["op"] == "above" else cur <= al["value"]


def alert_key(al):
    return (al["kind"], al.get("coin"), al.get("asset"), al.get("side"), al.get("method"))


async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not alerts:
        return
    cache = {}
    for al in list(alerts):
        k = alert_key(al)
        if k in cache:
            continue
        try:
            cache[k] = await asyncio.to_thread(get_value, al)
        except Exception as e:
            log.warning("price check failed: %s", type(e).__name__)
        await asyncio.sleep(1.5)
    fired = []
    for al in list(alerts):
        cur = cache.get(alert_key(al))
        if cur is not None and is_hit(al, cur):
            fired.append((al, cur))
    for al, cur in fired:
        w = "وصل" if al["op"] == "above" else "نزل"
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"🔔 {alert_title(al)} {w} لـ {money(cur)} {alert_unit(al)}\n"
                f"(التنبيه كان على {money(al['value'])})",
            )
        except Exception as e:
            log.warning("send failed: %s", type(e).__name__)
            continue
        if al in alerts:
            alerts.remove(al)
    if fired:
        save()


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


def main():
    if not TOKEN:
        print("CRYPTO_BOT_TOKEN مش موجود. اعمل export الأول.")
        return
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    if app.job_queue is None:
        print("job-queue مش متثبتة. نفّذ: pip install 'python-telegram-bot[job-queue]'")
        return
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("p2p", cmd_p2p))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^p2:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_alerts, interval=CHECK_EVERY, first=30)
    print("Crypto bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
