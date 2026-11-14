"""لقطة شاشة صفحة المنتج + مستطيلات على السعر والكوبونات + شريط القناة (لوجو + اسم القناة + اللينك).

- البحث عن أماكن السعر والكوبونات بيتم بقراءة النص من الصورة (tesseract). لو tesseract مش موجود أو معرفش يلاقي حاجة،
  بيتحط الشريط بس من غير مستطيلات (مفيش خطأ).
- على Termux:  pkg install tesseract
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import logging

from PIL import Image, ImageDraw

import deal_card as dc

log = logging.getLogger("shot_card")

MAX_W = 1080
SCALE = 2                      # بنكبّر الصورة قبل القراءة عشان الأرقام الصغيرة تتقري صح
PRICE_LINE = (220, 20, 60)      # أحمر
PRICE_FILL = (255, 214, 0, 70)  # أصفر شفاف
COUPON_LINE = (22, 150, 70)     # أخضر
COUPON_FILL = (60, 200, 90, 55)
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*\d{2,}$")
BANNER_H_RATIO = 100 / 1200.0   # ارتفاع الشريط نسبة للعرض (زي كروت العروض)


def _ocr_words(img):
    """يرجع قايمة كلمات [{t,l,t,w,h,conf}] بإحداثيات الصورة الأصلية. [] لو tesseract مش متاح."""
    exe = shutil.which("tesseract")
    if not exe:
        return []
    big = img.resize((img.width * SCALE, img.height * SCALE), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "in.png")
        big.save(p)
        try:
            r = subprocess.run([exe, p, "stdout", "-l", "eng", "--psm", "11", "tsv"],
                               capture_output=True, text=True, timeout=60)
        except Exception as e:
            log.warning("ocr failed: %s", type(e).__name__)
            return []
    words = []
    for line in r.stdout.splitlines()[1:]:
        c = line.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        if conf < 25:
            continue
        words.append({
            "t": c[11].strip(),
            "l": int(c[6]) / SCALE, "tp": int(c[7]) / SCALE,
            "w": int(c[8]) / SCALE, "h": int(c[9]) / SCALE,
            "conf": conf,
        })
    return words


def _digits(s):
    return re.sub(r"[^0-9,]", "", s)


PCT_RE = re.compile(r"^-?(\d{1,2})%-?$")


def find_price_box_by_pct(words, price, pct, top_limit):
    """لو الرقم نفسه اتقرا غلط (أرقام صغيرة): بنستخدم نسبة الخصم (-17%) كمرساة ونحسب مكان السعر على يسارها."""
    cands = []
    for w in words:
        m = PCT_RE.match(w["t"].replace("“", "").replace("”", ""))
        if not m or w["tp"] < top_limit:
            continue
        v = int(m.group(1))
        if not 3 <= v <= 90:
            continue
        if pct and abs(v - pct) > 1:
            continue
        cands.append(w)
    if not cands:
        return None
    a = min(cands, key=lambda w: w["tp"])        # أعلى واحدة = نسبة خصم المنتج (الكوبونات تحتها)
    n = len(f"{int(price):,}") if price else 4
    h = a["h"]
    x0 = a["l"] - (n * 0.62 * 1.15 * h + 3.6 * h + 8)
    return [x0, a["tp"] - 0.4 * h, a["l"] + a["w"] + 8, a["tp"] + 2.7 * h]


def price_candidates_by_color(im, price, top_limit):
    """آخر حل: نسبة الخصم "-17%" مكتوبة بالأحمر (كلام رفيع مش مربع متملي). بنلاقيها من لونها ونحسب مكان السعر على يسارها."""
    from PIL import ImageChops
    r, g, b = im.split()
    # أحمر/وردي غامق (حتى الحواف الناعمة للخط): R عالي وأعلى من G بكتير ومن B شوية
    m = ImageChops.multiply(ImageChops.multiply(r.point(lambda x: 255 if x > 120 else 0),
                                                ImageChops.subtract(r, g).point(lambda x: 255 if x > 60 else 0)),
                            ImageChops.subtract(r, b).point(lambda x: 255 if x > 30 else 0))
    raw, W = m.tobytes(), im.width
    start = None
    out = []
    for y in range(int(top_limit), im.height + 1):
        on = y < im.height and raw[y * W:(y + 1) * W].count(255) >= 3
        if on and start is None:
            start = y
        elif not on and start is not None:
            t, bt = start, y
            start = None
            if not 12 <= bt - t <= 42:
                continue
            sub = m.crop((0, t, W, bt))
            sb, bh = sub.tobytes(), bt - t
            cols = [x for x in range(W) if any(sb[yy * W + x] for yy in range(bh))]
            if not cols:
                continue
            # نقسّم الأعمدة الحمرا لتجمعات (بينهم فراغ) عشان صور المنتج الحمرا على اليمين متلخبطش
            groups, gs, gp = [], cols[0], cols[0]
            for x in cols[1:]:
                if x - gp > 14:
                    groups.append((gs, gp + 1))
                    gs = x
                gp = x
            groups.append((gs, gp + 1))
            for gx0, gx1 in groups:
                bw = gx1 - gx0
                if not 22 <= bw <= 100:
                    continue
                part = sub.crop((gx0, 0, gx1, bh))
                bb = part.getbbox()
                if not bb:
                    continue
                th = bb[3] - bb[1]
                fill = part.tobytes().count(255) / max(1, bw * th)
                if th < 10 or fill >= 0.5:
                    continue
                n = len(f"{int(price):,}") if price else 4
                tt = t + bb[1]
                x0 = gx0 - (n * 0.62 * 1.15 * th + 3.6 * th + 8)   # الأرقام + الكسور الصغيرة + كلمة جنيه
                out.append([x0, tt - 0.4 * th, gx1 + 8, tt + 2.7 * th])
    return out


def _crop_has_price(im, box, price):
    """نقص مستطيل السعر ونقراه تاني بتكبير كبير: لو فيه أرقام السعر يبقى هو."""
    exe = shutil.which("tesseract")
    if not exe or not price:
        return False
    x0, y0, x1, y1 = [int(v) for v in _clamp(box, im)]
    if x1 - x0 < 20 or y1 - y0 < 10:
        return False
    crop0 = im.crop((x0, y0, x1, y1))
    want = str(int(price))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "c.png")
        for scale in (3, 4, 6):
            crop0.resize((crop0.width * scale, crop0.height * scale), Image.LANCZOS).save(p)
            for psm in ("6", "11"):
                try:
                    r = subprocess.run([exe, p, "stdout", "-l", "eng", "--psm", psm], capture_output=True, text=True, timeout=30)
                except Exception:
                    return False
                if want in re.sub(r"\D", "", r.stdout):
                    return True
    return False


def find_price_box(words, price, pct=None, top_limit=0, im=None):
    """مستطيل حوالين السعر. بنجرّب أكتر من طريقة، وأي مستطيل مش مؤكد بنتأكد منه بقراءة المنطقة تاني؛ ولو مفيش مؤكد مفيش مستطيل."""
    if not price:
        return None
    box = _find_price_box_digits(words, price)
    if box:
        return box
    cands = []
    b = find_price_box_by_pct(words, price, pct, top_limit)
    if b and pct:
        return b          # قرينا نفس نسبة الخصم المعروفة من الصفحة (±1): مرساة موثوقة
    if b:
        cands.append(b)
    if im is not None:
        cands += price_candidates_by_color(im, price, top_limit)[:6]
    for b in cands:
        if im is None or _crop_has_price(im, b, price):
            return b
    return None


def _find_price_box_digits(words, price):
    want = f"{int(price):,}"
    cands = [w for w in words if _digits(w["t"]) == want or _digits(w["t"]).replace(",", "") == want.replace(",", "")]
    if not cands:
        return None
    # أكبر خط (السعر الأساسي أطول كلمة بارتفاع أكبر)
    w0 = max(cands, key=lambda w: w["h"])
    cy = w0["tp"] + w0["h"] / 2
    cx0 = w0["l"] + w0["w"] / 2
    row = [w for w in words if abs((w["tp"] + w["h"] / 2) - cy) < w0["h"] * 0.9
           and abs((w["l"] + w["w"] / 2) - cx0) < w0["w"] * 1.5]     # قريبة بس: "جنيه" و "-11%" من غير صورة المنتج
    x0 = min(min(w["l"] for w in row), w0["l"] - w0["w"] * 0.45) - 8   # مكان كلمة "جنيه"
    x1 = max(max(w["l"] + w["w"] for w in row), w0["l"] + w0["w"] * 1.7) + 8   # مكان نسبة الخصم
    y0 = w0["tp"] - w0["h"] * 0.35
    y1 = w0["tp"] + w0["h"] * 2.1   # يشمل سطر سعر القائمة تحت السعر
    return [x0, y0, x1, y1]


def _green_bands(im, x_limit, y_from):
    """شارات الكوبون الخضرا (زي "20%" و"توفير"): بترجّع [(top, bottom)] لكل شارة جوه العمود."""
    from PIL import ImageChops
    crop = im.crop((0, 0, max(10, int(x_limit)), im.height))
    h, s, v = crop.convert("HSV").split()
    m = ImageChops.multiply(ImageChops.multiply(h.point(lambda x: 255 if 58 <= x <= 100 else 0),
                                                s.point(lambda x: 255 if x >= 85 else 0)),
                            v.point(lambda x: 255 if x >= 165 else 0))
    raw = m.tobytes()
    W = crop.width
    min_px = max(14, W // 40)
    bands, start = [], None
    for y in range(int(y_from), crop.height):
        on = raw[y * W:(y + 1) * W].count(255) >= min_px
        if on and start is None:
            start = y
        elif not on and start is not None:
            if 10 <= y - start <= 48:
                bands.append((start, y))
            start = None
    return bands


def find_coupon_boxes(words, below_y, im=None, right_edge=None):
    """مستطيل لكل كوبون. الأساس هو الشارة الخضرا؛ والكود (حروف كبيرة + أرقام) بيوسّع المستطيل لو نزل سطر زيادة."""
    codes = [w for w in words if CODE_RE.match(w["t"]) and 5 <= len(w["t"]) <= 14 and w["tp"] > below_y]
    right = max([w["l"] + w["w"] for w in codes] + [right_edge or 0]) + 10
    if im is not None and right > 40:
        bands = _green_bands(im, right, below_y)
        if bands:
            boxes = []
            for i, (t, bt) in enumerate(bands):
                nxt = bands[i + 1][0] - 5 if i + 1 < len(bands) else 10 ** 6
                top = t - 6
                bottom = t + (bt - t) * 2.35
                inside = [w for w in codes if t - 4 <= w["tp"] < nxt]
                if inside:
                    bottom = max(bottom, max(w["tp"] + w["h"] for w in inside) + 6)
                boxes.append([2, top, right, min(bottom, nxt)])
            return boxes
    if not codes:
        return []
    codes.sort(key=lambda w: w["tp"])
    uniq = []
    for w in codes:
        if uniq and abs(w["tp"] - uniq[-1]["tp"]) < w["h"] * 1.5:
            if w["conf"] > uniq[-1]["conf"]:
                uniq[-1] = w
            continue
        uniq.append(w)
    boxes, prev_bottom = [], below_y
    for w in uniq:
        top = max(prev_bottom + w["h"] * 0.5, w["tp"] - w["h"] * 2.1)
        bottom = w["tp"] + w["h"] * 1.5
        boxes.append([2, top, right, bottom])
        prev_bottom = bottom
    return boxes


def find_rating_box(words, above_y):
    """صف التقييم: لازم رقم التقييم (4.2) وعدد المراجعات بين أقواس ((2,449)) على نفس السطر؛ عشان منمسحش أرقام من العنوان."""
    nums = [w for w in words if re.fullmatch(r"[1-5][.,]\d", w["t"]) and w["tp"] < above_y]
    counts = [w for w in words if re.fullmatch(r"\(\d[\d,.]*\)?", w["t"]) and w["tp"] < above_y]
    best = None
    for n in nums:
        cy = n["tp"] + n["h"] / 2
        for c in counts:
            if abs((c["tp"] + c["h"] / 2) - cy) < n["h"] * 0.8 and abs(c["l"] - n["l"]) < n["w"] * 12:
                best = (n, c)
    if not best:
        return None
    n, c = best
    x0 = min(n["l"], c["l"]) - 3
    x1 = max(n["l"] + n["w"], c["l"] + c["w"]) + 3
    top = min(n["tp"], c["tp"]) - 4
    return [x0, top, x1, max(n["tp"] + n["h"], c["tp"] + c["h"]) + 4]


def _clamp(box, im):
    x0, y0, x1, y1 = box
    return [max(0, int(x0)), max(0, int(y0)), min(im.width - 1, int(x1)), min(im.height - 1, int(y1))]


def _banner(card, bh):
    """شريط القناة بنفس شكل كروت العروض (نسبة للعرض): لوجو + اسم القناة + اللينك."""
    W, H = card.size
    k = W / 1200.0
    d = ImageDraw.Draw(card)
    d.rectangle([0, H - bh, W, H], fill=dc.NAVY)
    right = W - int(40 * k)
    mid = H - bh // 2
    if os.path.exists(dc.LOGO_FILE):
        try:
            lg = Image.open(dc.LOGO_FILE).convert("RGBA")
            dia = int(80 * k)
            lg.thumbnail((dia - int(6 * k), dia - int(6 * k)), Image.LANCZOS)
            d.ellipse([right - dia, mid - dia // 2, right, mid + dia // 2], fill=dc.WHITE)
            card.paste(lg, (right - dia + (dia - lg.width) // 2, mid - lg.height // 2), lg)
            right -= dia + int(18 * k)
        except Exception:
            pass
    bf = dc.font(max(14, int(42 * k)))
    bw = int(dc.width(d, dc.BRAND, bf)) + int(56 * k)
    ph = int(68 * k)
    d.rounded_rectangle([right - bw, mid - ph // 2, right, mid + ph // 2], radius=int(18 * k), fill=dc.YELLOW)
    dc.draw_text(d, (right - bw // 2, mid), dc.BRAND, bf, dc.NAVY, "mm")
    d.text((int(40 * k), mid), dc.LINK, font=dc.font(max(12, int(34 * k))), fill=dc.WHITE, anchor="lm")


def make_shot(raw, price=None, pct=None, crop_top=0, crop_bottom=0):
    """raw: بايتات لقطة الشاشة. يرجع (JPEG بايتات, {"price": bool, "coupons": عدد, "rating_hidden": bool})."""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    if crop_top or crop_bottom:
        im = im.crop((0, crop_top, im.width, im.height - crop_bottom))
    if im.width > MAX_W:
        im = im.resize((MAX_W, int(im.height * MAX_W / im.width)), Image.LANCZOS)
    info = {"price": False, "coupons": 0, "rating_hidden": False}
    try:
        words = _ocr_words(im)
        pbox = find_price_box(words, price, pct, im.height * 0.12, im) if words else None
        below = pbox[3] if pbox else 0
        rbox = find_rating_box(words, pbox[1] if pbox else im.height * 0.4) if words else None
        cboxes = find_coupon_boxes(words, below, im, (pbox[2] if pbox else None)) if words else []

        if rbox:   # شيل التقييمات
            ImageDraw.Draw(im).rectangle(_clamp(rbox, im), fill=(255, 255, 255))
            info["rating_hidden"] = True
        ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        lw = max(4, im.width // 200)
        if pbox:
            od.rounded_rectangle(_clamp(pbox, im), radius=8, fill=PRICE_FILL, outline=PRICE_LINE + (255,), width=lw)
            info["price"] = True
        for b in cboxes:
            od.rounded_rectangle(_clamp(b, im), radius=8, fill=COUPON_FILL, outline=COUPON_LINE + (255,), width=lw)
        info["coupons"] = len(cboxes)
        im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    except Exception as e:
        log.warning("marking failed: %s", type(e).__name__)

    bh = max(60, int(im.width * BANNER_H_RATIO))
    card = Image.new("RGB", (im.width, im.height + bh), (255, 255, 255))
    card.paste(im, (0, 0))
    _banner(card, bh)
    out = io.BytesIO()
    card.save(out, "JPEG", quality=92)
    return out.getvalue(), info
