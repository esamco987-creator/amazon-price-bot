"""
بيعمل كارت صورة للعرض: صورة المنتج + نسبة الخصم + السعر + السعر القديم + العنوان.
بياناته من أمازون، والتصميم بتاعنا (من غير شعار أمازون).
لو أي حاجة ناقصة (خط/مكتبة) بيرجع None والبوت يستخدم الصورة العادية.
المكتبات المطلوبة: pip install arabic-reshaper python-bidi
"""
import io
import os
import logging

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:  # المكتبات مش متثبتة
    arabic_reshaper = None
    get_display = None

log = logging.getLogger("card")

BASE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE, "fonts")
FONT_FILE = os.path.join(FONT_DIR, "Tajawal-Bold.ttf")
FONT_URLS = [
    "https://github.com/google/fonts/raw/main/ofl/tajawal/Tajawal-Bold.ttf",
    "https://raw.githubusercontent.com/google/fonts/main/ofl/tajawal/Tajawal-Bold.ttf",
]
FALLBACK_FONTS = [
    "/system/fonts/NotoNaskhArabic-Bold.ttf",
    "/system/fonts/NotoNaskhArabic-Regular.ttf",
    "/system/fonts/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

NAVY = (14, 16, 51)
YELLOW = (255, 201, 60)
RED = (220, 38, 38)
GREEN = (22, 130, 70)
GRAY = (110, 110, 125)
DARK = (25, 25, 40)
WHITE = (255, 255, 255)

SIZE = 1080
BRAND = "عروض وخصومات"
LINK = "t.me/PromoCodeOnlain"
LOGO_FILE = os.path.join(BASE, "logo.png")

_font_path = {"p": None}


def ensure_font():
    if _font_path["p"]:
        return _font_path["p"]
    if os.path.exists(FONT_FILE) and os.path.getsize(FONT_FILE) > 10000:
        _font_path["p"] = FONT_FILE
        return FONT_FILE
    os.makedirs(FONT_DIR, exist_ok=True)
    for u in FONT_URLS:
        try:
            r = requests.get(u, timeout=40)
            if r.status_code == 200 and len(r.content) > 10000:
                with open(FONT_FILE, "wb") as f:
                    f.write(r.content)
                _font_path["p"] = FONT_FILE
                return FONT_FILE
        except Exception as e:
            log.warning("font download failed: %s", type(e).__name__)
    for p in FALLBACK_FONTS:
        if os.path.exists(p):
            _font_path["p"] = p
            return p
    return None


def font(size):
    # لو احنا اللي بنشكّل العربي (reshaper + bidi) لازم نقفل تشكيل Pillow الداخلي،
    # وإلا الحروف بتتشكّل مرتين وتطلع مقلوبة ومفكوكة.
    if arabic_reshaper and get_display:
        return ImageFont.truetype(ensure_font(), size, layout_engine=ImageFont.Layout.BASIC)
    return ImageFont.truetype(ensure_font(), size)


def shape(text):
    """يجهّز النص العربي للرسم (تشكيل الحروف + اتجاه من اليمين)."""
    if arabic_reshaper and get_display:
        return get_display(arabic_reshaper.reshape(text))
    return text


def width(draw, text, f):
    return draw.textlength(shape(text), font=f)


def draw_text(draw, xy, text, f, fill, anchor="ra"):
    draw.text(xy, shape(text), font=f, fill=fill, anchor=anchor)


def wrap(draw, text, f, max_w, max_lines):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if width(draw, t, f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and width(draw, last + "…", f) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last + "…"
    return lines


def money(v):
    s = f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    return s


EVENT_LABEL = "عرض خاص"   # الشارة العادية


def current_label(item):
    """الشارة زي ما أمازون كاتباها للمنتج (لو موجودة)، وإلا الشارة العادية."""
    return (item.get("badge") or "").strip() or EVENT_LABEL


W, H = 1200, 800
INK = (15, 17, 17)
MUTED = (86, 89, 89)
CRIMSON = (204, 12, 57)


def make_card(item, raw_image):
    """كارت أفقي: صورة المنتج يمين، والبيانات شمالها، وشريط القناة تحت.
    item فيه title, price, old, pct. يرجع JPEG بايتات أو None."""
    try:
        if not ensure_font() or not raw_image:
            return None
        card = Image.new("RGB", (W, H), WHITE)
        d = ImageDraw.Draw(card)

        # شريط علوي رفيع
        d.rectangle([0, 0, W, 16], fill=NAVY)
        d.rectangle([0, 16, W, 22], fill=YELLOW)

        # صورة المنتج على اليمين
        prod = Image.open(io.BytesIO(raw_image)).convert("RGBA")
        bg = Image.new("RGBA", prod.size, (255, 255, 255, 255))
        prod = Image.alpha_composite(bg, prod).convert("RGB")
        area_w, area_h = 540, 610
        prod.thumbnail((area_w, area_h), Image.LANCZOS)
        card.paste(prod, (W - 40 - area_w + (area_w - prod.width) // 2, 50 + (area_h - prod.height) // 2))

        # عمود البيانات على اليسار (محاذاة لليمين)
        rx = 640
        colw = 590
        tf = font(48)
        lines = wrap(d, item["title"], tf, colw, 3)
        y = 60
        for ln in lines:
            draw_text(d, (rx, y), ln, tf, INK, "ra")
            y += 64

        # الشارة
        lt = current_label(item)
        lf = font(34)
        lw = int(width(d, lt, lf)) + 44
        ly = max(y + 24, 290)
        d.rounded_rectangle([rx - lw, ly, rx, ly + 52], radius=8, fill=CRIMSON)
        draw_text(d, (rx - lw // 2, ly + 26), lt, lf, WHITE, "mm")

        # صف السعر: -% ثم السعر (مع الكسور صغيرة فوق) ثم جنيه
        by = ly + 150  # خط الأساس
        pf = font(96)
        sf = font(40)
        pct_txt = f"-{item['pct']}%"
        pctf = font(56)
        d.text((rx, by), pct_txt, font=pctf, fill=CRIMSON, anchor="rs")
        x = rx - d.textlength(pct_txt, font=pctf) - 28

        price = round(item["price"], 2)
        whole = int(price)
        frac = int(round((price - whole) * 100))
        if frac:
            ft = f"{frac:02d}"
            d.text((x, by - 52), ft, font=sf, fill=INK, anchor="rs")
            x -= d.textlength(ft, font=sf) + 4
        wt = f"{whole:,}"
        d.text((x, by), wt, font=pf, fill=INK, anchor="rs")
        x -= d.textlength(wt, font=pf) + 14
        draw_text(d, (x, by - 8), "جنيه", font(34), INK, "rs")

        # سعر القائمة
        gy = by + 62
        of = font(34)
        lab = "سعر القائمة:"
        draw_text(d, (rx, gy), lab, of, MUTED, "rs")
        x = rx - width(d, lab, of) - 12
        old_txt = f"{money(item['old'])} جنيه"
        ow = width(d, old_txt, of)
        draw_text(d, (x, gy), old_txt, of, MUTED, "rs")
        d.line([x - ow, gy - 12, x, gy - 12], fill=MUTED, width=3)

        # التوفير
        save = item["old"] - item["price"]
        draw_text(d, (rx, gy + 56), f"وفّر {money(save)} جنيه", font(38), GREEN, "rs")

        # شريط القناة تحت
        d.rectangle([0, H - 100, W, H], fill=NAVY)
        right = W - 40
        if os.path.exists(LOGO_FILE):
            try:
                lg = Image.open(LOGO_FILE).convert("RGBA")
                lg.thumbnail((74, 74), Image.LANCZOS)
                d.ellipse([right - 80, H - 90, right, H - 10], fill=WHITE)
                card.paste(lg, (right - 80 + (80 - lg.width) // 2, H - 90 + (80 - lg.height) // 2), lg)
                right -= 98
            except Exception:
                pass
        bf = font(42)
        bw = int(width(d, BRAND, bf)) + 56
        d.rounded_rectangle([right - bw, H - 84, right, H - 16], radius=18, fill=YELLOW)
        draw_text(d, (right - bw // 2, H - 50), BRAND, bf, NAVY, "mm")
        d.text((40, H - 50), LINK, font=font(34), fill=WHITE, anchor="lm")

        out = io.BytesIO()
        card.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("card failed: %s %s", type(e).__name__, e)
        return None
