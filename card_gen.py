"""card_gen.py — генератор PNG-карточек для броней зала «Муза»."""
import io

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

W, H = 800, 460

# Цветовая схема Муза (светлая, как в приложении)
BG     = (246, 239, 228)   # #F6EFE4 — тёплый кремовый
BG2    = (237, 227, 212)   # #EDE3D4 — шапка
GOLD   = (196, 168, 130)   # #C4A882 — золото
TEXT   = (42,  30,  20)    # #2A1E14 — тёмный шоколад
HINT   = (122, 98,  72)    # muted brown
ACCENT = (139, 96,  32)    # gold text
PHONE_CLR = (180, 100, 40) # orange-gold для телефона
DIV    = (196, 175, 148)   # цвет разделителей

_FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _fmt_date(dt_str: str) -> str:
    try:
        d, m, y = dt_str.split(".")
        return f"{int(d)} {_RU_MONTHS[int(m)-1]} {y}"
    except Exception:
        return dt_str


def _fmt_money(v) -> str:
    n = int(float(v or 0))
    if n == 0:
        return ""
    return f"{n:,} ₽".replace(",", " ")  # неразрывный пробел


def generate_booking_card(booking: dict) -> bytes | None:
    """Генерирует PNG-карточку брони. Возвращает bytes или None."""
    if not _PIL_OK:
        return None

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Загружаем шрифты
    def _f(path, size):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    f_muza  = _f(_FONT_B, 22)
    f_sub   = _f(_FONT_R, 13)
    f_date  = _f(_FONT_B, 38)
    f_wd    = _f(_FONT_R, 13)
    f_lbl   = _f(_FONT_R, 11)
    f_val   = _f(_FONT_B, 17)
    f_phone = _f(_FONT_B, 16)
    f_foot  = _f(_FONT_R, 10)

    # ── Шапка ────────────────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (W, 100)], fill=BG2)
    draw.rectangle([(0, 0), (5, H)], fill=GOLD)      # золотая полоса слева
    draw.line([(28, 100), (W-28, 100)], fill=DIV)    # разделитель

    # «МУЗА»
    draw.text((28, 18), "МУЗА", font=f_muza, fill=GOLD)
    draw.text((110, 22), "·  БАНКЕТНЫЙ ЗАЛ", font=f_sub, fill=HINT)

    # Дата
    date_display = _fmt_date(booking.get("date", ""))
    draw.text((28, 52), date_display, font=f_date, fill=TEXT)

    # Пилюля с днём недели (рядом с датой)
    wd = booking.get("weekday", "")
    if wd:
        try:
            dw = int(draw.textlength(date_display, font=f_date))
        except Exception:
            dw = 290
        px = 28 + dw + 12
        draw.rounded_rectangle([(px, 60), (px+80, 82)], radius=11, fill=(220, 208, 190))
        draw.text((px + 40, 71), wd, font=f_wd, fill=ACCENT, anchor="mm")

    # ── Вспомогательные функции ───────────────────────────────────────────────
    def lbl(x, y, txt):
        draw.text((x, y), txt.upper(), font=f_lbl, fill=HINT)

    def val(x, y, txt, color=TEXT, font=f_val):
        draw.text((x, y), txt or "—", font=font, fill=color)

    def hdiv(y):
        draw.line([(28, y), (W-28, y)], fill=DIV)

    def vdiv(x, y1, y2):
        draw.line([(x, y1), (x, y2)], fill=DIV)

    # ── Блок 1: Клиент / Гости / Источник ────────────────────────────────────
    y = 118
    lbl(28, y, "Клиент")
    val(28, y+17, booking.get("name") or "—")

    vdiv(220, y, y+54)
    lbl(234, y, "Гостей")
    val(234, y+17, str(booking.get("guests") or "—"))

    vdiv(320, y, y+54)
    lbl(334, y, "Источник")
    val(334, y+17, booking.get("source") or "—")

    y += 62
    hdiv(y); y += 10

    # ── Блок 2: Телефон / Договор ─────────────────────────────────────────────
    lbl(28, y, "Телефон")
    phone = booking.get("phone") or "—"
    val(28, y+17, phone, color=PHONE_CLR, font=f_phone)

    vdiv(260, y, y+54)
    lbl(274, y, "Договор")
    val(274, y+17, booking.get("contract_date") or "—")

    y += 62
    hdiv(y); y += 10

    # ── Блок 3: Аренда / Меню / Итого ────────────────────────────────────────
    rent  = float(booking.get("revenue_rent") or 0)
    menu  = float(booking.get("revenue_menu") or 0)
    total = rent + menu

    lbl(28, y, "Аренда")
    val(28, y+17, _fmt_money(rent) or "—")

    vdiv(180, y, y+54)
    lbl(194, y, "Меню")
    val(194, y+17, _fmt_money(menu) or "—")

    if total > 0:
        vdiv(340, y, y+54)
        lbl(354, y, "Итого")
        draw.text((354, y+17), _fmt_money(total), font=f_val, fill=ACCENT)

    y += 62
    hdiv(y); y += 10

    # ── Комментарий ──────────────────────────────────────────────────────────
    comment = (booking.get("comment") or "").replace("[Голос] ", "")
    if comment:
        lbl(28, y, "Комментарий")
        if len(comment) > 70:
            comment = comment[:67] + "..."
        draw.text((28, y+17), comment, font=f_sub, fill=HINT)

    # ── Футер ────────────────────────────────────────────────────────────────
    hdiv(H - 28)
    draw.text((28, H - 20), "muza.booking", font=f_foot, fill=DIV)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf.read()
