"""
utils.py — общие вспомогательные функции для bot.py и avito_poll.py.
"""

import re
from datetime import date

# ─── Словари месяцев ──────────────────────────────────────────────────────────

MONTHS = {
    "января": 1, "январь": 1, "янв": 1,
    "февраля": 2, "февраль": 2, "фев": 2,
    "марта": 3, "март": 3, "мар": 3,
    "апреля": 4, "апрель": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6, "июн": 6,
    "июля": 7, "июль": 7, "июл": 7,
    "августа": 8, "август": 8, "авг": 8,
    "сентября": 9, "сентябрь": 9, "сен": 9,
    "октября": 10, "октябрь": 10, "окт": 10,
    "ноября": 11, "ноябрь": 11, "ноя": 11,
    "декабря": 12, "декабрь": 12, "дек": 12,
}

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март",
    4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь",
    10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

MONTHS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# ─── Парсинг дат ──────────────────────────────────────────────────────────────

def parse_date(text: str) -> date | None:
    """Извлекает дату из произвольного текста. Возвращает date или None."""
    t = text.lower()
    today = date.today()

    # дд.мм / дд/мм / дд.мм.гг / дд.мм.гггг
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        yr_raw = m.group(3)
        yr = (int(yr_raw) + (2000 if int(yr_raw) < 100 else 0)) if yr_raw else today.year
        try:
            c = date(yr, mon, day)
            if not yr_raw and c < today:
                c = date(yr + 1, mon, day)
            return c
        except ValueError:
            pass

    # YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # дд месяц [год]
    pat = r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b"
    m = re.search(pat, t)
    if m:
        day, mon = int(m.group(1)), MONTHS[m.group(2)]
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            c = date(yr, mon, day)
            if not m.group(3) and c < today:
                c = date(yr + 1, mon, day)
            return c
        except ValueError:
            pass

    return None


def parse_all_dates(text: str) -> list[date]:
    """Извлекает все даты из текста (клиент может написать несколько сразу)."""
    t = text.lower()
    today = date.today()
    found: list[date] = []
    seen: set[tuple] = set()

    def _add(d: date | None):
        if d and (d.month, d.day) not in seen:
            seen.add((d.month, d.day))
            found.append(d)

    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t):
        day, mon = int(m.group(1)), int(m.group(2))
        yr_raw = m.group(3)
        yr = (int(yr_raw) + (2000 if int(yr_raw) < 100 else 0)) if yr_raw else today.year
        try:
            c = date(yr, mon, day)
            if not yr_raw and c < today:
                c = date(yr + 1, mon, day)
            _add(c)
        except ValueError:
            pass

    pat = r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b"
    for m in re.finditer(pat, t):
        day, mon = int(m.group(1)), MONTHS[m.group(2)]
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            c = date(yr, mon, day)
            if not m.group(3) and c < today:
                c = date(yr + 1, mon, day)
            _add(c)
        except ValueError:
            pass

    return found


# ─── Телефон / контакты ───────────────────────────────────────────────────────

_PHONE_RE = re.compile(
    r"(\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|\b\d{11}\b"
    r"|\b[789]\d{9}\b)"
)

_TG_NICK_RE = re.compile(r"@[a-zA-Z][a-zA-Z0-9_]{3,}")


def normalize_phone(phone: str) -> str:
    """
    Нормализует номер телефона до цифр: '8 (900) 123-45-67' → '79001234567'.
    Используется для дедупликации лидов в database.py и sheets.py.
    """
    import re as _re
    digits = _re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    return digits


def has_phone(text: str) -> bool:
    return bool(_PHONE_RE.search(text))


def extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text)
    return m.group(0) if m else ""


def fmt_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith(("7", "8")):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return raw


def has_tg_nick(text: str) -> bool:
    return bool(_TG_NICK_RE.search(text))


def extract_tg_nick(text: str) -> str:
    m = _TG_NICK_RE.search(text)
    return m.group(0) if m else ""
