"""
بيعمل كارت صورة للعرض: صورة المنتج + نسبة الخصم + السعر + السعر القديم + العنوان.
بياناته من أمازون، والتصميم بتاعنا (من غير شعار أمازون).
لو أي حاجة ناقصة (خط/مكتبة) بيرجع None والبوت يستخدم الصورة العادية.
المكتبات المطلوبة: pip install arabic-reshaper python-bidi
"""
import io
import os
import math
import logging

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:  # المكتبات مش متثبتة
    arabic_reshaper = None
    get_display = None

try:
    from PIL import features as _features
    HAS_RAQM = bool(_features.check("raqm"))
except Exception:
    HAS_RAQM = False
# لو Pillow عنده raqm بيشكّل العربي لوحده صح. الطريقة التانية (reshaper + bidi) احتياطي.
USE_MANUAL = (not HAS_RAQM) and bool(arabic_reshaper and get_display)

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
    if USE_MANUAL:
        return ImageFont.truetype(ensure_font(), size, layout_engine=ImageFont.Layout.BASIC)
    return ImageFont.truetype(ensure_font(), size)


def shape(text):
    """يجهّز النص العربي للرسم (تشكيل الحروف + اتجاه من اليمين)."""
    if USE_MANUAL:
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
    return f"{int(v):,}" if v == int(v) else f"{v:,.2f}"


EVENT_LABEL = "عرض خاص"   # الشارة العادية


def current_label(item):
    """الشارة زي ما أمازون كاتباها للمنتج (لو موجودة)، وإلا الشارة العادية."""
    return (item.get("badge") or "").strip() or EVENT_LABEL


W, H = 1200, 800
INK = (15, 17, 17)
MUTED = (86, 89, 89)
CRIMSON = (204, 12, 57)


STAR = (245, 166, 35)
STAR_OFF = (214, 217, 222)
LINKBLUE = (22, 94, 148)
COUPON_BG = (236, 247, 240)
COUPON_BORDER = (170, 214, 188)


def _star_mask(size):
    """قناع نجمة (مرسوم بحجم كبير وبعدين متصغّر عشان الحواف تطلع ناعمة)."""
    big = size * 4
    m = Image.new("L", (big, big), 0)
    pts = []
    for i in range(10):
        r = big / 2 if i % 2 == 0 else big / 2 * 0.42
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((big / 2 + r * math.cos(a), big / 2 + r * math.sin(a) + big * 0.03))
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def draw_stars(card, right, cy, rating, size=34, gap=4):
    """5 نجوم من اليمين لليسار، متعبّية بنسبة التقييم. يرجع x لآخر نجمة (الشمال)."""
    mask = _star_mask(size)
    x = right
    for i in range(5):
        fill = max(0.0, min(1.0, rating - i))
        x0 = x - size
        y0 = cy - size // 2
        card.paste(Image.new("RGB", (size, size), STAR_OFF), (x0, y0), mask)
        if fill > 0:
            w = max(1, int(size * fill))
            part = Image.new("RGB", (w, size), STAR)
            # التعبئة من اليمين لليسار
            card.paste(part, (x - w, y0), mask.crop((size - w, 0, size, size)))
        x = x0 - gap
    return x + gap


def draw_rating(card, d, rx, y, rating, reviews):
    """صف التقييم: رقم التقييم + نجوم + عدد التقييمات."""
    nf = font(34)
    rt = f"{rating:.1f}"
    d.text((rx, y), rt, font=nf, fill=INK, anchor="rm")
    x = rx - d.textlength(rt, font=nf) - 12
    x = draw_stars(card, int(x), y, rating)
    if reviews:
        d.text((x - 14, y), f"({reviews:,})", font=font(32), fill=LINKBLUE, anchor="rm")


def draw_coupon(card, d, rx, y, text):
    """شريط كوبون بلونه: [الكوبون:] + نص الكوبون."""
    tag = "الكوبون:"
    tf, bf = font(30), font(33)
    tw = int(width(d, tag, tf)) + 34
    avail = 590 - tw - 66
    line = f"{text}  •  فعّل الكوبون من صفحة المنتج"
    if width(d, line, bf) > avail:
        line = f"{text}  •  فعّل الكوبون"
    if width(d, line, bf) > avail:
        line = text
    bw = min(int(width(d, line, bf)) + 40, avail)
    h = 60
    x0 = rx - tw - bw - 56
    d.rounded_rectangle([x0 - 10, y - 8, rx + 10, y + h + 8], radius=12, fill=COUPON_BG, outline=COUPON_BORDER, width=3)
    d.rounded_rectangle([rx - tw, y, rx, y + h], radius=8, fill=YELLOW)
    draw_text(d, (rx - tw // 2, y + h // 2), tag, tf, NAVY, "mm")
    # علامة صح
    cx, cy2 = rx - tw - 34, y + h // 2
    d.ellipse([cx - 17, cy2 - 17, cx + 17, cy2 + 17], fill=GREEN)
    d.line([cx - 8, cy2, cx - 2, cy2 + 7, cx + 9, cy2 - 7], fill=WHITE, width=5, joint="curve")
    lines = wrap(d, line, bf, bw + 20, 1)
    draw_text(d, (rx - tw - 62, y + h // 2), lines[0], bf, (22, 100, 58), "rm")


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
        tf = font(46)
        lines = wrap(d, item["title"], tf, colw, 3)
        y = 60
        for ln in lines:
            draw_text(d, (rx, y), ln, tf, INK, "ra")
            y += 62

        shift = 0

        # الشارة
        lt = current_label(item)
        lf = font(31)
        lw = int(width(d, lt, lf)) + 44
        ly = max(y + 22 + shift, 250)
        d.rounded_rectangle([rx - lw, ly, rx, ly + 52], radius=8, fill=CRIMSON)
        draw_text(d, (rx - lw // 2, ly + 26), lt, lf, WHITE, "mm")

        # صف السعر: -% ثم السعر (مع الكسور صغيرة فوق) ثم جنيه
        by = ly + 128  # خط الأساس
        pf = font(72)
        sf = font(32)
        x = rx
        if item.get("pct"):
            pct_txt = f"-{item['pct']}%"
            pctf = font(46)
            d.text((rx, by), pct_txt, font=pctf, fill=CRIMSON, anchor="rs")
            x = rx - d.textlength(pct_txt, font=pctf) - 28

        price = round(item["price"], 2)
        whole = int(price)
        frac = int(round((price - whole) * 100))
        if frac:
            ft = f"{frac:02d}"
            d.text((x, by - 40), ft, font=sf, fill=INK, anchor="rs")
            x -= d.textlength(ft, font=sf) + 4
        wt = f"{whole:,}"
        d.text((x, by), wt, font=pf, fill=INK, anchor="rs")
        x -= d.textlength(wt, font=pf) + 10
        draw_text(d, (x, by - 6), "جنيه", font(30), INK, "rs")

        gy = by
        if item.get("old") and item["old"] > item["price"]:
            # سعر القائمة
            gy = by + 56
            of = font(31)
            lab = "سعر القائمة:"
            draw_text(d, (rx, gy), lab, of, MUTED, "rs")
            x = rx - width(d, lab, of) - 12
            old_txt = f"{money(item['old'])} جنيه"
            ow = width(d, old_txt, of)
            draw_text(d, (x, gy), old_txt, of, MUTED, "rs")
            d.line([x - ow, gy - 12, x, gy - 12], fill=MUTED, width=3)

            # التوفير
            save = item["old"] - item["price"]
            draw_text(d, (rx, gy + 54), f"وفّر {money(save)} جنيه", font(34), GREEN, "rs")
            gy += 54

        coupon = (item.get("coupon") or "").strip()
        if coupon:
            draw_coupon(card, d, rx, min(gy + 34, H - 100 - 82), coupon)

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


def _footer(card, d):
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


def make_promo_card(title, images, dates=None):
    """كارت لعرض كمية (اشتري 3 بسعر 2 ...): عنوان العرض + كولاج صور من منتجاته.
    images: قايمة بايتات صور. يرجع JPEG بايتات أو None."""
    try:
        if not ensure_font() or not images:
            return None
        card = Image.new("RGB", (W, H), WHITE)
        d = ImageDraw.Draw(card)
        d.rectangle([0, 0, W, 16], fill=NAVY)
        d.rectangle([0, 16, W, 22], fill=YELLOW)

        # كولاج الصور على اليمين
        imgs = []
        for raw in images[:4]:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                imgs.append(Image.alpha_composite(bg, im).convert("RGB"))
            except Exception:
                continue
        if not imgs:
            return None
        gx0, gy0, gw, gh = W - 40 - 560, 50, 560, 610
        if len(imgs) == 1:
            cells = [(gx0, gy0, gw, gh)]
        elif len(imgs) == 2:
            cells = [(gx0, gy0, gw, gh // 2 - 6), (gx0, gy0 + gh // 2 + 6, gw, gh // 2 - 6)]
        else:
            cw, ch = gw // 2 - 6, gh // 2 - 6
            cells = [(gx0, gy0, cw, ch), (gx0 + cw + 12, gy0, cw, ch),
                     (gx0, gy0 + ch + 12, cw, ch), (gx0 + cw + 12, gy0 + ch + 12, cw, ch)]
        for im, (x, y, cw, ch) in zip(imgs, cells):
            d.rounded_rectangle([x, y, x + cw, y + ch], radius=14, outline=(225, 225, 232), width=3)
            im.thumbnail((cw - 16, ch - 16), Image.LANCZOS)
            card.paste(im, (x + (cw - im.width) // 2, y + (ch - im.height) // 2))

        # عنوان العرض على الشمال
        rx = 575
        lt = "عرض خاص"
        lf = font(34)
        lw = int(width(d, lt, lf)) + 44
        d.rounded_rectangle([rx - lw, 70, rx, 122], radius=8, fill=CRIMSON)
        draw_text(d, (rx - lw // 2, 96), lt, lf, WHITE, "mm")
        tf = font(56)
        y = 170
        for ln in wrap(d, title, tf, 530, 4):
            draw_text(d, (rx, y), ln, tf, INK, "ra")
            y += 76
        if dates:
            draw_text(d, (rx, y + 20), dates, font(38), MUTED, "ra")
        draw_text(d, (rx, H - 150), "الخصم بيظهر في عربة التسوق", font(34), MUTED, "rs")

        _footer(card, d)
        out = io.BytesIO()
        card.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("promo card failed: %s %s", type(e).__name__, e)
        return None


def make_announce_card(bot_name="@Mix_Compare_bot", sample="سماعة ريدمي بودز 5"):
    """كارت إعلان محرك البحث (للبوست المثبّت في القناة). يرجع JPEG بايتات أو None."""
    try:
        if not ensure_font():
            return None
        card = Image.new("RGB", (W, H), WHITE)
        d = ImageDraw.Draw(card)
        d.rectangle([0, 0, W, 16], fill=NAVY)
        d.rectangle([0, 16, W, 22], fill=YELLOW)
        rx = W - 70

        lt = "جديد"
        lf = font(36)
        lw = int(width(d, lt, lf)) + 48
        d.rounded_rectangle([rx - lw, 58, rx, 112], radius=10, fill=CRIMSON)
        draw_text(d, (rx - lw // 2, 85), lt, lf, WHITE, "mm")

        draw_text(d, (rx, 135), "ابحث عن أي منتج", font(96), NAVY, "ra")
        draw_text(d, (rx, 262), "وشوف أحسن سعر وخصم عليه على أمازون", font(44), MUTED, "ra")

        # مربع بحث
        bx0, by0, bx1, by1 = 70, 360, W - 70, 470
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=55, fill=(244, 245, 250), outline=(205, 208, 222), width=4)
        draw_text(d, (bx1 - 40, (by0 + by1) // 2), sample, font(46), (120, 124, 140), "rm")
        cx, cy = bx0 + 70, (by0 + by1) // 2 - 6
        d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], outline=NAVY, width=8)
        d.line([cx + 16, cy + 16, cx + 42, cy + 42], fill=NAVY, width=9)

        # زر البوت
        bf = font(54)
        label = bot_name
        tw = int(d.textlength(label, font=bf)) + 90
        mid = W // 2
        d.rounded_rectangle([mid - tw // 2, 520, mid + tw // 2, 620], radius=22, fill=YELLOW)
        d.text((mid, 570), label, font=bf, fill=NAVY, anchor="mm")
        draw_text(d, (mid, 660), "للمشتركين في القناة بس • مجاني", font(34), MUTED, "mm")

        _footer(card, d)
        out = io.BytesIO()
        card.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("announce card failed: %s %s", type(e).__name__, e)
        return None
