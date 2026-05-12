"""
avito_poll.py — цикл опроса Авито + умные авто-ответы + уведомления в Telegram.

═══════════════════════════════════════════════════════════════
  ЛОГИКА АВТО-ОТВЕТОВ (в порядке приоритета):

  1. Мебель / декор / музыка / что входит в стоимость
           → FAQ_AMENITIES (мебель, скатерти, аудио, живая музыка, декор)

  2. Своя еда / алкоголь / пробковый сбор
           → FAQ_FOOD (свой кейтеринг, алкоголь без пробкового сбора)

  3. Цена / стоимость / аренда / условия
           → клиент пишет «здесь», «тут», «сюда» → FAQ_PRICE (полные условия)
           → иначе → REPLY_ASK_PHONE (просим телефон для консультации)

  4. Вместимость / сколько человек / мест
           → клиент хочет общаться здесь → FAQ_CAPACITY (60 / 80 фуршет)
           → иначе → REPLY_ASK_PHONE

  5. Получен контакт (телефон / @ник)
           → сохраняем лид в таблицу, благодарим, тегаем администратора

  6. Дата в сообщении → проверяем в таблице
           → свободна: просим телефон
           → занята:   предлагаем другие даты

  7. Первое сообщение → приветствие «Здравствуйте, Имя! Это зал «Муза»...»

  8. Повторное без даты → молчим, ждём ручного ответа менеджера

═══════════════════════════════════════════════════════════════
  УВЕДОМЛЕНИЕ В TELEGRAM-ГРУППУ:
  — Каждое НОВОЕ сообщение клиента → новое TG-сообщение (карточка с последними 8 репликами контекста)
  — Каждый авто-ответ бота → отдельное TG-сообщение (зеркало)
  — Если API не помечает чат как unread — догон: stale по известным чатам (AVITO_POLL_STALE_CHECK_EVERY)
    и broad — последние чаты без фильтра unread (AVITO_POLL_BROAD_CHATS_EVERY / _LIMIT)
  — Ручной ответ админа → новое TG-сообщение (зеркало)
  — РЕДАКТИРОВАНИЕ клиентом → обновляется именно та карточка
  — Реплай на любую карточку → текст уходит в Авито (bot.py, group=1)
═══════════════════════════════════════════════════════════════
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import date, datetime

import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from avito import AvitoClient
from sheets import add_lead

log = logging.getLogger("AVITO_POLL")
_e = html.escape

# check_date: быстрое чтение из SQLite, фоллбэк на Sheets
try:
    from database import check_date
    log.info("check_date → SQLite (быстрый кеш)")
except ImportError:
    from sheets import check_date
    log.info("check_date → Google Sheets")

# ─── Конфиг ──────────────────────────────────────────────────────────────────

POLL_INTERVAL      = int(os.getenv("AVITO_POLL_INTERVAL", "20"))
AVITO_NOTIFY_GROUP = int(os.getenv("AVITO_NOTIFY_GROUP_ID", "0"))
ADMIN_TG_USERNAME  = os.getenv("NOTIFY_USERNAME", "")
# Раз в N циклов опроса догоняем новые входящие в уже известных чатах (см. приветствие).
# Нужно, когда ответ клиента не переводит чат в «непрочитанный» в API Авито.
# 0 — отключить.
STALE_CHECK_EVERY  = int(os.getenv("AVITO_POLL_STALE_CHECK_EVERY", "2"))
# Раз в M циклов — запрашиваем последние чаты без фильтра unread (и новые лиды).
# Ловит диалоги, которые не в «непрочитанных» и ещё не в состоянии бота.
# 0 — отключить. Лимит — сколько чатов за один такой запрос (каждый потом get_messages).
BROAD_CHATS_EVERY    = int(os.getenv("AVITO_POLL_BROAD_CHATS_EVERY", "3"))
BROAD_CHATS_LIMIT    = int(os.getenv("AVITO_POLL_BROAD_CHATS_LIMIT", "50"))

STATE_FILE = os.path.join(os.path.dirname(__file__), "bot_state.json")

# ─── Тексты авто-ответов (plain text — Авито не поддерживает HTML) ───────────

REPLY_FREE = (
    "Эта дата свободна! 🤍\n\n"
    "Пришлите, пожалуйста, Ваш номер телефона или Телеграм — "
    "пришлём презентацию и условия.\n"
    "Наш Телеграм: @muza_zal"
)

REPLY_BUSY = "Эта дата, к сожалению, уже занята. Давайте рассмотрим другие даты?"

REPLY_THANKS = "Спасибо! С вами свяжется наш менеджер. 🤍"

# Просим телефон вместо ответа на вопрос о цене/вместимости
REPLY_ASK_PHONE = (
    "Чтобы рассчитать стоимость под ваше мероприятие, нашему менеджеру нужно "
    "уточнить несколько деталей.\n\n"
    "Пришлите, пожалуйста, номер телефона или Телеграм — "
    "свяжемся и всё расскажем!\n"
    "Наш Телеграм: @muza_zal"
)

REPLY_ASK_DATE = "Скажите, пожалуйста, какую дату рассматриваете? Проверим свободна ли она."

# Просим уточнить детали мероприятия перед тем как давать условия
REPLY_ASK_DETAILS = (
    "Чтобы подобрать оптимальные условия, уточните, пожалуйста:\n\n"
    "• Дату мероприятия\n"
    "• Количество гостей\n"
    "• Тип мероприятия (свадьба, день рождения, корпоратив и т.д.)\n\n"
    "Проверим свободные даты и подготовим всё для вас! 🤍"
)

# FAQ отвечаем только если клиент хочет общаться здесь (не даёт номер)
FAQ_PRICE = (
    "Условия аренды банкетного зала «Муза»:\n\n"
    "• Аренда площадки — 60 000 ₽\n"
    "• Депозит — 250 000 ₽ (заказ по кухне)\n"
    "• Обслуживание — 15%\n"
    "• Алкоголь — свой, без пробкового сбора\n\n"
    "Для подробной консультации: @muza_zal  ·  +79253579000"
)

FAQ_CAPACITY = (
    "Вместимость банкетного зала «Муза»:\n\n"
    "• До 60 гостей — банкет / ужин\n"
    "• До 80 гостей — фуршет\n\n"
    "Для уточнения деталей: @muza_zal  ·  +79253579000"
)

FAQ_AMENITIES = (
    "В стоимость аренды входит:\n\n"
    "• Мебель — столы и стулья\n"
    "• Декор — белые скатерти и салфетки\n"
    "• Аудиосистема — профессиональная звуковая система\n"
    "• Живая музыка — разрешена\n"
    "• Караоке — можно организовать через наших подрядчиков\n\n"
    "Если есть вопросы — пишите: @muza_zal"
)

FAQ_FOOD = (
    "У нас собственный кейтеринг — вкусная и разнообразная кухня, "
    "которую мы с удовольствием подберём под ваш праздник.\n\n"
    "Алкоголь можно привезти свой — пробкового сбора нет.\n\n"
    "Для знакомства с меню: @muza_zal  ·  +79253579000"
)

# ─── Состояние сессии ────────────────────────────────────────────────────────

_start_ts:         float           = 0.0
_last_handled:     dict[str, str]  = {}  # avito_chat_id → последний обработанный avito_msg_id
_msg_content:      dict[str, str]  = {}  # avito_msg_id  → текст (для определения редактирования)
_msg_id_to_tg:     dict[str, int]  = {}  # avito_msg_id  → tg_msg_id (для редактирования карточки)
_greeted_chats:    set[str]        = set()
_awaiting_contact: set[str]        = set()
_asked_date:       set[str]        = set()   # чаты где уже спросили дату повторно
_lead_received:    set[str]        = set()   # чаты где уже получили контакт

# Контекст по чату: накапливаем дату/гостей/мероприятие между сообщениями
_chat_context: dict[str, dict] = {}
# Структура: {"date": date|None, "guests": str, "event": str}

# Диагностика: разовое предупреждение о «тихом» отключении TG
_logged_tg_skip_no_group: bool = False
# Сколько подряд циклов поллера без непрочитанных чатов (для логов)
_poll_idle_cycles: int = 0


# ─── Сохранение / загрузка состояния ─────────────────────────────────────────

def _load_state() -> None:
    """Загружает состояние из файла при старте бота."""
    global _greeted_chats, _last_handled, _msg_content, _awaiting_contact, _asked_date, _lead_received
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _greeted_chats    = set(data.get("greeted_chats", []))
        _last_handled     = data.get("last_handled", {})
        _msg_content      = data.get("msg_content", {})
        _awaiting_contact = set(data.get("awaiting_contact", []))
        _asked_date       = set(data.get("asked_date", []))
        _lead_received    = set(data.get("lead_received", []))
        log.info("✅ Состояние загружено: %d чатов", len(_greeted_chats))
    except FileNotFoundError:
        log.info("ℹ️  Файл состояния не найден — старт с чистого листа")
    except Exception as e:
        log.warning("⚠️  Не удалось загрузить состояние: %s", e)


def _save_state() -> None:
    """Сохраняет состояние в файл."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "greeted_chats":    list(_greeted_chats),
                "last_handled":     _last_handled,
                "msg_content":      _msg_content,
                "awaiting_contact": list(_awaiting_contact),
                "asked_date":       list(_asked_date),
                "lead_received":    list(_lead_received),
            }, f, ensure_ascii=False)
    except Exception as e:
        log.warning("⚠️  Не удалось сохранить состояние: %s", e)


# ─── Ключевые слова ──────────────────────────────────────────────────────────

_PRICE_KEYWORDS    = {"цена","цены","стоимость","сколько стоит","прайс",
                      "расценки","почём","почем","аренда стоит",
                      "цена аренды","сколько стоит аренда","условия аренды",
                      "депозит","обслуживание","аренда зала"}

_CAPACITY_KEYWORDS = {"вместимость","вместить","вмещает","сколько человек",
                      "человек максимум","вместит","максимум человек",
                      "мест","на сколько","сколько мест","людей"}

_AMENITIES_KEYWORDS = {"мебель","столы","стулья","декор","скатерт","салфетк",
                       "аудио","колонки","музыка","звук","звуков","звуковая",
                       "живая музыка","живой звук","своя музыка",
                       "что входит","что включено","что в стоимость",
                       "что есть в зале","оборудование"}

_KARAOKE_KEYWORDS  = {"караоке"}

_FOOD_KEYWORDS     = {"кейтеринг","своя еда","своя кухня","свою еду",
                      "можно ли привезти","привезти еду","своё питание",
                      "пробковый сбор","пробкового сбора","алкоголь свой",
                      "свой алкоголь","можно алкоголь","алкоголь можно"}

# Клиент хочет общаться в Авито
_PREFERS_HERE_KEYWORDS = {
    "напишите сюда", "пишите сюда", "скажите сюда", "отвечайте сюда",
    "напишите здесь", "пишите здесь", "скажите здесь", "отвечайте здесь",
    "общаться тут", "общаться здесь", "общаться в авито",
    "скажите тут", "напишите тут", "пишите тут",
    "не дам номер", "не хочу давать номер", "не буду звонить",
    "в авито пишите", "отвечайте в авито",
}

_EVENT_TYPES       = ["свадьб","день рождения","корпоратив","юбилей","банкет",
                      "вечеринк","праздник","торжеств","мероприяти","выпускной",
                      "помолвк","крестин","детский праздник","фуршет"]

_MONTHS = {
    "января":1,"январь":1,"янв":1,
    "февраля":2,"февраль":2,"фев":2,
    "марта":3,"март":3,"мар":3,
    "апреля":4,"апрель":4,"апр":4,
    "мая":5,"май":5,
    "июня":6,"июнь":6,"июн":6,
    "июля":7,"июль":7,"июл":7,
    "августа":8,"август":8,"авг":8,
    "сентября":9,"сентябрь":9,"сен":9,
    "октября":10,"октябрь":10,"окт":10,
    "ноября":11,"ноябрь":11,"ноя":11,
    "декабря":12,"декабрь":12,"дек":12,
}

_MONTHS_SHORT = {
    1:"янв",2:"фев",3:"мар",4:"апр",5:"май",6:"июн",
    7:"июл",8:"авг",9:"сен",10:"окт",11:"ноя",12:"дек",
}


# ─── Функции определения содержимого сообщения ───────────────────────────────

def _is_price_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _PRICE_KEYWORDS)


def _is_capacity_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _CAPACITY_KEYWORDS)


def _is_amenities_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _AMENITIES_KEYWORDS) or any(kw in t for kw in _KARAOKE_KEYWORDS)


def _is_food_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _FOOD_KEYWORDS)


def _prefers_avito_chat(text: str) -> bool:
    """Клиент явно хочет общаться здесь, не хочет давать номер."""
    t = text.lower()
    return any(kw in t for kw in _PREFERS_HERE_KEYWORDS)


def _has_guests(text: str) -> bool:
    return bool(re.search(
        r'\b(\d+)\s*(человек|гостей|гостя|чел\.?|люд[яей]|персон|гост)',
        text.lower()
    ))


def _has_event_type(text: str) -> bool:
    t = text.lower()
    return any(ev in t for ev in _EVENT_TYPES)


def _has_phone(text: str) -> bool:
    return bool(re.search(
        r'(\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'  # +7/8 xxx xxx xx xx
        r'|\b\d{11}\b'                                                          # 11 цифр подряд
        r'|\b[789]\d{9}\b)',                                                    # 10 цифр с 7/8/9
        text,
    ))


def _extract_phone(text: str) -> str:
    m = re.search(
        r'\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
        r'|\b\d{11}\b'
        r'|\b[789]\d{9}\b',
        text,
    )
    return m.group(0) if m else ""


def _has_tg_nick(text: str) -> bool:
    return bool(re.search(r'@[a-zA-Z][a-zA-Z0-9_]{3,}', text))


def _extract_tg_nick(text: str) -> str:
    m = re.search(r'@[a-zA-Z][a-zA-Z0-9_]{3,}', text)
    return m.group(0) if m else ""


def _parse_date(text: str) -> date | None:
    """Извлекает дату из произвольного текста."""
    t = text.lower()
    today = date.today()

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

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    pat = r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?\b"
    m = re.search(pat, t)
    if m:
        day, mon = int(m.group(1)), _MONTHS[m.group(2)]
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            c = date(yr, mon, day)
            if not m.group(3) and c < today:
                c = date(yr + 1, mon, day)
            return c
        except ValueError:
            pass

    return None


def _parse_all_dates(text: str) -> list[date]:
    """Извлекает все даты из текста (для случая когда клиент пишет две даты)."""
    t = text.lower()
    today = date.today()
    found: list[date] = []
    seen: set[tuple] = set()

    def _add(d: date | None):
        if d and (d.month, d.day) not in seen:
            seen.add((d.month, d.day))
            found.append(d)

    # дд.мм / дд/мм / дд.мм.гг
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

    # дд месяц [год]
    pat = r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?\b"
    for m in re.finditer(pat, t):
        day, mon = int(m.group(1)), _MONTHS[m.group(2)]
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            c = date(yr, mon, day)
            if not m.group(3) and c < today:
                c = date(yr + 1, mon, day)
            _add(c)
        except ValueError:
            pass

    return found


# ─── Персональное приветствие ─────────────────────────────────────────────────

def _build_greeting(buyer_name: str, msg_text: str, chat_id: str = "") -> str:
    name = buyer_name.strip() if buyer_name and buyer_name not in ("", "Клиент") else ""
    hi = f"Здравствуйте, {name}!" if name else "Здравствуйте!"

    # Берём контекст из предыдущих сообщений (если есть)
    ctx = _chat_context.get(chat_id, {})

    missing = []
    if not _parse_date(msg_text) and not ctx.get("date"):
        missing.append("дату")
    if not _has_guests(msg_text) and not ctx.get("guests"):
        missing.append("количество гостей")
    if not _has_event_type(msg_text) and not ctx.get("event"):
        missing.append("мероприятие")

    if not missing:
        return f"{hi} Это банкетный зал «Муза». Сейчас проверю свободные даты!"

    if len(missing) == 1:
        ask = missing[0]
    elif len(missing) == 2:
        ask = f"{missing[0]} и {missing[1]}"
    else:
        ask = f"{missing[0]}, {missing[1]} и {missing[2]}"

    return (
        f"{hi} Это банкетный зал «Муза». Я помогу проверить свободные даты.\n\n"
        f"Скажите, пожалуйста, {ask}."
    )


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _fmt_ts(ts: int) -> str:
    """Полный формат для истории: дд.мм чч:мм"""
    if not ts:
        return "??"
    try:
        dt = datetime.fromtimestamp(ts)
        return f"{dt.day:02d}.{dt.month:02d} {dt.strftime('%H:%M')}"
    except Exception:
        return "??"


def _fmt_price(price) -> str:
    if not price:
        return ""
    val = price.get("value", 0) if isinstance(price, dict) else int(price)
    if not val:
        return ""
    return f"{val:,}".replace(",", " ") + " ₽"


def _find_buyer(users: list[dict], uid_self: int) -> dict:
    for u in users:
        if u.get("id") != uid_self:
            return u
    return {}


def _find_self(users: list[dict], uid_self: int) -> dict:
    for u in users:
        if u.get("id") == uid_self:
            return u
    return {}


def _raw_incoming_text(msg: dict) -> str:
    """Текст из поля content (подпись к фото и т.д.) — для парсинга дат и ключевых слов."""
    content = msg.get("content")
    if not isinstance(content, dict):
        return ""
    return (content.get("text") or "").strip()


def _display_text_for_msg(msg: dict) -> str:
    """
    Текст для карточки TG и для сравнения «редактирование / дубликат».
    Если тела нет — человекочитаемая метка по типу сообщения Авито.
    """
    raw = _raw_incoming_text(msg)
    if raw:
        return raw
    mtype = (msg.get("type") or "unknown").lower()
    labels = {
        "text":       "[текст без содержимого]",
        "image":      "[изображение]",
        "photo":      "[изображение]",
        "voice":      "[голосовое сообщение]",
        "audio":      "[аудио]",
        "video":      "[видео]",
        "file":       "[файл]",
        "link":       "[ссылка]",
        "location":   "[геолокация]",
        "call":       "[звонок]",
        "system":     "[системное]",
    }
    if mtype in labels:
        return labels[mtype]
    return f"[сообщение:{mtype}]"


def _incoming_type_stats(messages: list[dict]) -> str:
    """Краткая сводка типов последних сообщений (для отладки в логах)."""
    counts: dict[str, int] = {}
    for m in messages[:15]:
        d = m.get("direction", "?")
        t = str(m.get("type") or "?")
        key = f"{d}:{t}"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


# ─── Форматирование карточек ─────────────────────────────────────────────────

def _format_client_msg(
    chat_info:    dict,
    msg:          dict,
    uid_self:     int,
    account_name: str,
    all_messages: list[dict] | None = None,
    *,
    include_meta: bool = False,
    edited:       bool = False,
) -> str:
    """
    Карточка входящего сообщения клиента.
    all_messages — полный список сообщений чата (для истории).
    include_meta — добавляем объявление и профиль.
    edited       — клиент отредактировал сообщение.
    """
    users      = chat_info.get("users", [])
    buyer      = _find_buyer(users, uid_self)
    buyer_name = buyer.get("name", "Клиент")
    buyer_url  = (buyer.get("public_user_profile") or {}).get("url", "")
    msg_display = _display_text_for_msg(msg)
    ts_str     = _fmt_ts(msg.get("created", 0))
    edit_mark  = "  <i>✏️ изменено</i>" if edited else ""
    current_id = msg.get("id", "")

    lines = []

    # ── Новое сообщение — вверху, имя жирное, без даты ───────────────────────
    lines.append(f"<b>Авито · {_e(account_name)}</b>")
    lines.append(f"<b>{_e(buyer_name)}:</b> {_e(msg_display[:400])}{edit_mark}")

    # ── История последних 8 сообщений — ниже, дд.мм чч:мм Имя: текст ────────
    if all_messages:
        history = [
            m for m in all_messages
            if m.get("id") != current_id
        ]
        history.sort(key=lambda m: m.get("created", 0))
        history = history[-8:]

        if history:
            lines.append("")
            for m in history:
                ts  = _fmt_ts(m.get("created", 0))
                out = m.get("direction") == "out"
                nm  = account_name if out else buyer_name
                txt = _display_text_for_msg(m)
                lines.append(f"<code>{ts}</code>  {_e(nm)}: {_e(txt[:120])}")

    # ── Мета (объявление, профиль) ────────────────────────────────────────────
    if include_meta:
        ctx_val   = chat_info.get("context", {}).get("value", {})
        ad_title  = ctx_val.get("title", "")
        ad_url    = ctx_val.get("url", "")
        ad_price  = _fmt_price(ctx_val.get("price"))
        location  = (ctx_val.get("location") or {}).get("name", "")

        meta = []
        if buyer_url:
            meta.append(f'<a href="{buyer_url}">{_e(buyer_name)}</a>')
        if ad_title:
            price_sfx = f" · {ad_price}" if ad_price else ""
            if ad_url:
                meta.append(f'<a href="{ad_url}">{_e(ad_title)}</a>{_e(price_sfx)}')
            else:
                meta.append(f"{_e(ad_title)}{_e(price_sfx)}")
        if location:
            meta.append(_e(location))
        if meta:
            lines.append("")
            lines.append("  ".join(meta))

    return "\n".join(lines)


def _format_bot_msg(account_name: str, text: str, *, is_auto: bool = True) -> str:
    """Карточка ответа бота или администратора."""
    icon  = "🤖" if is_auto else "✏️"
    label = "Авто-ответ" if is_auto else "Ответ администратора"
    lines = [
        f"{icon} <b>{label} → Авито — {_e(account_name)}</b>",
        f"<i>{_e(text[:500])}</i>",
    ]
    return "\n".join(lines)


def _build_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Дата свободна", callback_data=f"av_free:{chat_id}"),
            InlineKeyboardButton("❌ Дата занята",   callback_data=f"av_busy:{chat_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Написать ответ", callback_data=f"av_hint:{chat_id}"),
        ],
    ])


# ─── Отправка / редактирование TG-сообщений ──────────────────────────────────

async def _send_tg_msg(
    application,
    avito_chat_id: str,
    avito_msg_id:  str | None,
    text:          str,
    keyboard:      InlineKeyboardMarkup | None = None,
) -> int | None:
    """
    Отправляет НОВОЕ сообщение в TG-группу.
    Сохраняет маппинги:
      tg_to_avito[tg_msg_id]      = avito_chat_id   (для реплаев)
      _msg_id_to_tg[avito_msg_id] = tg_msg_id       (для редактирования)
    """
    global _logged_tg_skip_no_group
    if not AVITO_NOTIFY_GROUP:
        if not _logged_tg_skip_no_group:
            _logged_tg_skip_no_group = True
            log.warning(
                "   ↳ TG: карточки Авито не отправляются — "
                "AVITO_NOTIFY_GROUP_ID не задан или равен 0 (проверьте .env на сервере)"
            )
        return None

    tg_to_avito: dict[int, str] = application.bot_data.setdefault("tg_to_avito", {})

    try:
        msg = await application.bot.send_message(
            chat_id=AVITO_NOTIFY_GROUP,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        tg_id = msg.message_id
        tg_to_avito[tg_id] = avito_chat_id
        if avito_msg_id:
            _msg_id_to_tg[avito_msg_id] = tg_id
        log.info("   ↳ 📤 TG msg=%d отправлено", tg_id)
        return tg_id
    except Exception as e:
        log.error("   ↳ ОШИБКА send TG: %s", e)
        return None


async def _update_tg_msg(
    application,
    avito_msg_id: str,
    new_text:     str,
    keyboard:     InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Редактирует существующую TG-карточку (вызывается при редактировании
    клиентом своего сообщения в Авито).
    """
    if not AVITO_NOTIFY_GROUP:
        return False

    tg_id = _msg_id_to_tg.get(avito_msg_id)
    if not tg_id:
        return False

    try:
        await application.bot.edit_message_text(
            chat_id=AVITO_NOTIFY_GROUP,
            message_id=tg_id,
            text=new_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        log.info("   ↳ 📝 TG msg=%d обновлено (редактирование)", tg_id)
        return True
    except Exception as e:
        log.error("   ↳ ОШИБКА edit TG: %s", e)
        return False


async def _sync_missed_inbound_for_chat_ids(
    client:        AvitoClient,
    application,
    uid_self:      int,
    account_name:  str,
    chat_ids:      set[str],
    *,
    log_label:     str = "sync",
) -> int:
    """
    Для каждого chat_id: если последнее входящее новее, чем _last_handled — _process_chat.
    """
    caught = 0
    for chat_id in list(chat_ids):
        if not chat_id:
            continue
        try:
            messages = await client.get_messages(chat_id, limit=20)
        except Exception as e:
            log.warning("[%s] %s get_messages %s: %s", account_name, log_label, chat_id[:16], e)
            continue

        incoming = [m for m in messages if m.get("direction") == "in"]
        if not incoming:
            continue

        last_in = max(incoming, key=lambda m: m.get("created", 0))
        mid = str(last_in.get("id", ""))
        if not mid:
            continue

        prev = str(_last_handled.get(chat_id, ""))
        if mid == prev:
            continue

        log.info(
            "[%s] 🔄 %s: chat=%s… новый in_msg=%s… (было %s)",
            account_name,
            log_label,
            chat_id[:16],
            mid[:24],
            prev[:24] if prev else "—",
        )
        try:
            await _process_chat(
                client, {"id": chat_id}, application, uid_self, account_name,
            )
            caught += 1
        except Exception as e:
            log.error(
                "[%s] %s _process_chat %s: %s",
                account_name, log_label, chat_id[:16], e,
                exc_info=True,
            )

    if caught:
        log.info("[%s] %s: догнано чатов с новым сообщением: %d", account_name, log_label, caught)
    return caught


async def _sync_missed_inbound_for_known_chats(
    client:       AvitoClient,
    application,
    uid_self:     int,
    account_name: str,
) -> None:
    """
    Известные чаты (уже в диалоге с ботом): догон при отсутствии unread в API.
    """
    candidates = set(_greeted_chats) | set(_last_handled.keys())
    if not candidates:
        return
    await _sync_missed_inbound_for_chat_ids(
        client, application, uid_self, account_name, candidates, log_label="stale-check",
    )


async def _sync_missed_from_broad_chat_list(
    client:       AvitoClient,
    application,
    uid_self:     int,
    account_name: str,
) -> None:
    """Последние N чатов объявлений без фильтра unread — новые и старые диалоги."""
    try:
        broad = await client.get_chats(unread_only=False, limit=BROAD_CHATS_LIMIT)
    except Exception as e:
        log.warning("[%s] broad-check get_chats: %s", account_name, e)
        return
    ids = {c.get("id") for c in (broad or []) if c.get("id")}
    if not ids:
        return
    log.info("[%s] broad-check: чатов в выборке API = %d", account_name, len(ids))
    await _sync_missed_inbound_for_chat_ids(
        client, application, uid_self, account_name, ids, log_label="broad-check",
    )


# ─── Обработка одного чата ───────────────────────────────────────────────────

async def _process_chat(
    client:       AvitoClient,
    chat:         dict,
    application,
    uid_self:     int,
    account_name: str,
) -> None:
    chat_id = chat.get("id", "")
    if not chat_id:
        return

    try:
        chat_info = await client.get_chat(chat_id)
    except Exception as e:
        log.error("[%s] get_chat %s: %s", account_name, chat_id, e)
        chat_info = chat

    try:
        messages = await client.get_messages(chat_id, limit=20)
    except Exception as e:
        log.error("[%s] get_messages %s: %s", account_name, chat_id, e)
        return

    log.info(
        "   [dbg] messages=%d, chat=%s… | сводка типов: %s",
        len(messages), chat_id[:16], _incoming_type_stats(messages),
    )
    if not messages:
        log.info("   [dbg] → нет сообщений, пропуск")
        return

    # Все входящие от клиента (не только text — иначе голос/фото теряются)
    incoming = [m for m in messages if m.get("direction") == "in"]
    log.info(
        "   [dbg] incoming_in=%d (все типы), из них с непустым text=%d",
        len(incoming),
        sum(1 for m in incoming if _raw_incoming_text(m)),
    )
    if not incoming:
        log.info("   [dbg] → нет входящих сообщений от клиента, пропуск (mark_read)")
        try:
            await client.mark_read(chat_id)
        except Exception:
            pass
        return

    # Самое новое входящее по времени
    last_in = max(incoming, key=lambda m: m.get("created", 0))
    msg_id   = last_in.get("id", "")
    msg_ts   = last_in.get("created", 0)
    msg_kind = last_in.get("type", "?")
    body_for_card = _display_text_for_msg(last_in)
    msg_text = _raw_incoming_text(last_in)

    log.info(
        "   [dbg] last_in id=%s type=%s ts=%s | тело для карточки=%r | сырой text для логики=%r",
        str(msg_id)[:24], msg_kind, msg_ts, body_for_card[:120], msg_text[:120],
    )
    log.info(
        "   [dbg] last_in ts=%s _start_ts=%s diff=%+.0f сек",
        msg_ts, int(_start_ts), msg_ts - _start_ts,
    )

    # Пропускаем сообщения до запуска бота
    if msg_ts < _start_ts:
        log.info("   [dbg] → сообщение старее запуска, пропуск (только учёт last_handled)")
        if _last_handled.get(chat_id) != msg_id:
            _last_handled[chat_id] = msg_id
            _msg_content[msg_id]   = body_for_card
            try:
                await client.mark_read(chat_id)
            except Exception:
                pass
        return

    buyer      = _find_buyer(chat_info.get("users", []), uid_self)
    buyer_name = buyer.get("name", "")

    # ══ Определяем: новое сообщение или редактирование ══════════════════════

    if _last_handled.get(chat_id) == msg_id:
        # Тот же msg_id — дубликат опроса или редактирование
        if _msg_content.get(msg_id) == body_for_card:
            log.info(
                "   [dbg] пропуск: то же входящее msg_id=%s (уже обработано, тело не менялось)",
                str(msg_id)[:24],
            )
            return
        # ── Клиент отредактировал сообщение ─────────────────────────────────
        log.info(
            "[%s] ✏️ Редактирование: %s | «%s»",
            account_name, buyer_name or "?", body_for_card[:80],
        )
        _msg_content[msg_id] = body_for_card

        edited_card = _format_client_msg(
            chat_info, last_in, uid_self, account_name, messages,
            include_meta=True, edited=True,
        )
        await _update_tg_msg(application, msg_id, edited_card, keyboard=_build_keyboard(chat_id))
        try: await client.mark_read(chat_id)
        except: pass
        return

    # ══ Новое входящее сообщение ═════════════════════════════════════════════

    log.info(
        "[%s] 💬 %s | type=%s | «%s»",
        account_name, buyer_name or "?", msg_kind, body_for_card[:80],
    )
    _msg_content[msg_id] = body_for_card

    client_card = _format_client_msg(
        chat_info, last_in, uid_self, account_name, messages,
        include_meta=True,
    )
    await _send_tg_msg(
        application, chat_id, msg_id, client_card,
        keyboard=_build_keyboard(chat_id),
    )

    # ══ Обновляем контекст чата ══════════════════════════════════════════════

    ctx = _chat_context.setdefault(chat_id, {"date": None, "guests": "", "event": ""})
    if not ctx["date"]:
        d_found = _parse_date(msg_text)
        if d_found:
            ctx["date"] = d_found
    if not ctx["guests"] and _has_guests(msg_text):
        ctx["guests"] = msg_text
    if not ctx["event"] and _has_event_type(msg_text):
        ctx["event"] = msg_text

    # ══ Определяем авто-ответы ═══════════════════════════════════════════════

    auto_replies: list[str] = []

    prefers_here     = _prefers_avito_chat(msg_text)
    is_first         = chat_id not in _greeted_chats
    lead_already_got = chat_id in _lead_received

    # ── Вспомогательная функция: проверка дат (используется в двух местах) ──
    async def _check_dates_reply(dates: list) -> None:
        if len(dates) == 1:
            d = dates[0]
            log.info("   ↳ Дата: %s", d.strftime("%d.%m.%Y"))
            try:
                result = check_date(d)
                if result["found"]:
                    log.info("   ↳ ЗАНЯТО")
                    auto_replies.append(REPLY_BUSY)
                else:
                    log.info("   ↳ СВОБОДНО")
                    auto_replies.append(REPLY_FREE)
                    _awaiting_contact.add(chat_id)
            except Exception as e:
                log.error("   ↳ check_date: %s", e, exc_info=True)
                log.warning(
                    "   ↳ ответ «свободно/занято» не отправлен — ошибка check_date (см. выше)"
                )
        else:
            lines = []
            any_free = False
            for d in dates:
                try:
                    result = check_date(d)
                    day_str = d.strftime("%d.%m.%Y")
                    if result["found"]:
                        lines.append(f"• {day_str} — занята")
                    else:
                        lines.append(f"• {day_str} — свободна ✅")
                        any_free = True
                except Exception as e:
                    log.error("   ↳ check_date %s: %s", d, e)
            if lines:
                reply = "Проверила ваши даты:\n\n" + "\n".join(lines)
                if any_free:
                    reply += (
                        "\n\nПришлите, пожалуйста, Ваш номер телефона или Телеграм — "
                        "пришлём презентацию и условия.\nНаш Телеграм: @muza_zal"
                    )
                    _awaiting_contact.add(chat_id)
                else:
                    reply += "\n\nДавайте рассмотрим другие даты?"
                auto_replies.append(reply)

    # ── Первое сообщение ─────────────────────────────────────────────────────
    if is_first:
        log.info("   ↳ Первое сообщение — приветствие")
        auto_replies.append(_build_greeting(buyer_name, msg_text, chat_id))
        _greeted_chats.add(chat_id)
        # Если в первом сообщении уже есть дата — проверяем сразу
        first_dates = _parse_all_dates(msg_text)
        if first_dates:
            log.info("   ↳ Первое сообщение содержит дату — проверяем")
            _asked_date.discard(chat_id)
            await _check_dates_reply(first_dates)

    # ── Лид уже получен → отвечаем свободно на любые вопросы ────────────────
    elif lead_already_got:
        log.info("   ↳ Лид получен — отвечаем свободно")
        if _is_amenities_question(msg_text):
            auto_replies.append(FAQ_AMENITIES)
        elif _is_food_question(msg_text):
            auto_replies.append(FAQ_FOOD)
        elif _is_price_question(msg_text):
            auto_replies.append(FAQ_PRICE)
        elif _is_capacity_question(msg_text):
            auto_replies.append(FAQ_CAPACITY)
        else:
            dates = _parse_all_dates(msg_text)
            if dates:
                log.info("   ↳ лид есть, найдены даты — проверяем календарь")
                await _check_dates_reply(dates)
            else:
                log.info(
                    "   ↳ лид есть, текст без распознанных дат — молчим (ведёт менеджер)"
                )

    # ── FAQ мебель/декор — отвечаем всегда + спрашиваем дату ────────────────
    elif _is_amenities_question(msg_text):
        log.info("   ↳ FAQ: мебель/декор/музыка")
        reply = FAQ_AMENITIES
        if not ctx.get("date") and chat_id not in _awaiting_contact:
            reply += "\n\nКстати, на какую дату рассматриваете зал? Проверим наличие! 🗓"
        auto_replies.append(reply)

    # ── FAQ еда/алкоголь — отвечаем всегда + спрашиваем дату ───────────────
    elif _is_food_question(msg_text):
        log.info("   ↳ FAQ: своя еда/алкоголь")
        reply = FAQ_FOOD
        if not ctx.get("date") and chat_id not in _awaiting_contact:
            reply += "\n\nКстати, на какую дату рассматриваете зал? Проверим наличие! 🗓"
        auto_replies.append(reply)

    # ── FAQ цена ─────────────────────────────────────────────────────────────
    elif _is_price_question(msg_text):
        if prefers_here:
            # Отвечаем на вопрос И всё равно просим телефон
            log.info("   ↳ FAQ: цена + просим телефон (клиент хочет общаться здесь)")
            auto_replies.append(FAQ_PRICE)
            auto_replies.append(REPLY_ASK_PHONE)
        else:
            log.info("   ↳ Цена → просим телефон")
            auto_replies.append(REPLY_ASK_PHONE)

    # ── FAQ вместимость ──────────────────────────────────────────────────────
    elif _is_capacity_question(msg_text):
        if prefers_here:
            # Отвечаем на вопрос И всё равно просим телефон
            log.info("   ↳ FAQ: вместимость + просим телефон (клиент хочет общаться здесь)")
            auto_replies.append(FAQ_CAPACITY)
            auto_replies.append(REPLY_ASK_PHONE)
        else:
            log.info("   ↳ Вместимость → просим телефон")
            auto_replies.append(REPLY_ASK_PHONE)

    # ── Получен контакт ──────────────────────────────────────────────────────
    elif _has_phone(msg_text) or _has_tg_nick(msg_text):
        phone = _extract_phone(msg_text) if _has_phone(msg_text) else ""
        nick  = _extract_tg_nick(msg_text) if _has_tg_nick(msg_text) else ""
        log.info("   ↳ Получен контакт: phone=%s nick=%s", phone, nick)
        try:
            add_lead(name=buyer_name or "Авито", phone=phone, nick=nick, source=account_name)
            log.info("   ↳ Лид сохранён в таблицу")
        except Exception as e:
            log.error("   ↳ ОШИБКА add_lead: %s", e, exc_info=True)
        _awaiting_contact.discard(chat_id)
        _lead_received.add(chat_id)
        auto_replies.append(REPLY_THANKS)

        if AVITO_NOTIFY_GROUP and ADMIN_TG_USERNAME:
            contact_line = phone or nick
            try:
                await application.bot.send_message(
                    chat_id=AVITO_NOTIFY_GROUP,
                    text=(
                        f"📞 {ADMIN_TG_USERNAME}, новый лид с Авито!\n"
                        f"👤 {_e(buyer_name or 'Клиент')}\n"
                        f"📱 {_e(contact_line)}"
                    ),
                    parse_mode="HTML",
                )
                log.info("   ↳ Уведомление администратора отправлено")
            except Exception as e:
                log.warning("   ↳ Не удалось уведомить админа: %s", e)

    # ── Дата или повторное без даты ──────────────────────────────────────────
    else:
        dates = _parse_all_dates(msg_text)
        if dates:
            _asked_date.discard(chat_id)
            log.info("   ↳ найдено дат: %d — проверяем", len(dates))
            await _check_dates_reply(dates)
        else:
            if not msg_text.strip():
                log.info(
                    "   ↳ нет текста для авто-логики (напр. только вложение type=%s) — "
                    "FAQ/дату распознать нельзя",
                    msg_kind,
                )
            # Повторное сообщение без даты — спрашиваем один раз
            if chat_id not in _asked_date:
                log.info("   ↳ повторное без даты — спрашиваем дату")
                auto_replies.append(REPLY_ASK_DATE)
                _asked_date.add(chat_id)
            else:
                log.info(
                    "   ↳ повторное без даты — дату уже спрашивали; ждём менеджера или текст с датой"
                )

    # ══ Отправляем авто-ответы в Авито + зеркалируем в TG ═══════════════════

    if not auto_replies:
        log.info(
            "   ↳ итог: автоответов 0 (type=%s, текст для парсинга len=%d, "
            "лид=%s, уже_спрашивали_дату=%s)",
            msg_kind,
            len(msg_text),
            lead_already_got,
            chat_id in _asked_date,
        )
    for auto_reply in auto_replies:
        try:
            await client.send_message(chat_id, auto_reply)
            log.info("   ↳ ✅ Авто-ответ отправлен")
            bot_card = _format_bot_msg(account_name, auto_reply, is_auto=True)
            await _send_tg_msg(application, chat_id, None, bot_card)
        except Exception as e:
            log.error("   ↳ ОШИБКА send_message: %s", e)

    # ══ Сохраняем состояние ══════════════════════════════════════════════════

    _last_handled[chat_id] = msg_id
    _save_state()
    try:
        await client.mark_read(chat_id)
    except Exception as e:
        log.warning("   ↳ mark_read: %s", e)


# ─── Главный цикл ────────────────────────────────────────────────────────────

async def avito_polling_loop(client: AvitoClient, application) -> None:
    """
    Бесконечный цикл опроса Авито.
    Запускается как asyncio.Task из bot.py (post_init).
    """
    global _start_ts, _poll_idle_cycles
    _start_ts = time.time()
    _poll_iteration = 0

    _load_state()

    application.bot_data["avito_client"] = client
    application.bot_data.setdefault("tg_to_avito", {})

    log.info(
        "[%s] 🚀 Поллер запущен (интервал %d сек; stale каждые %s; broad %s / лимит %d)",
        client.name,
        POLL_INTERVAL,
        STALE_CHECK_EVERY if STALE_CHECK_EVERY else "—",
        BROAD_CHATS_EVERY if BROAD_CHATS_EVERY else "—",
        BROAD_CHATS_LIMIT,
    )
    if not AVITO_NOTIFY_GROUP:
        log.warning("[%s] ⚠️  AVITO_NOTIFY_GROUP_ID не задан", client.name)

    try:
        uid_self = await client.get_user_id()
    except Exception as e:
        log.error("[%s] Ошибка user_id: %s — поллер не запущен", client.name, e)
        return

    _net_fails = 0  # счётчик последовательных сетевых ошибок

    while True:
        _poll_iteration += 1
        try:
            # Retry при сетевых проблемах: до 3 попыток с паузой
            chats = None
            for attempt in range(3):
                try:
                    chats = await client.get_chats(unread_only=True)
                    _net_fails = 0  # сброс счётчика при успехе
                    break
                except (httpx.ConnectTimeout, httpx.ReadTimeout,
                        httpx.ConnectError, httpx.RemoteProtocolError) as net_err:
                    wait = 15 * (attempt + 1)
                    log.warning("[%s] ⚠️  Сетевая ошибка (попытка %d/3): %s — жду %d сек",
                                client.name, attempt + 1, net_err.__class__.__name__, wait)
                    if attempt < 2:
                        await asyncio.sleep(wait)
                    else:
                        _net_fails += 1
                        log.error("[%s] Авито недоступен (%d раз подряд) — пропускаю цикл",
                                  client.name, _net_fails)

            # Каждый цикл — видно в логах, что поллер жив (grep «опрос:»)
            if chats is not None:
                log.info("[%s] опрос: непрочитанных чатов = %d", client.name, len(chats))
            else:
                log.warning(
                    "[%s] опрос: get_chats вернул None (все попытки без успешного ответа)",
                    client.name,
                )

            if chats:
                _poll_idle_cycles = 0
                for chat in chats:
                    try:
                        await _process_chat(client, chat, application, uid_self, client.name)
                    except Exception as e:
                        log.error("[%s] Ошибка чата %s: %s",
                                  client.name, chat.get("id","?"), e, exc_info=True)
            else:
                _poll_idle_cycles += 1
                # None — не удалось получить список; [] — успех, просто нет непрочитанных
                if chats is None:
                    if _poll_idle_cycles == 1 or _poll_idle_cycles % 5 == 0:
                        log.warning(
                            "[%s] 📭 get_chats вернул None (сеть/ошибка после ретраев), цикл #%d",
                            client.name, _poll_idle_cycles,
                        )
                elif _poll_idle_cycles == 1 or _poll_idle_cycles % 10 == 0:
                    log.info(
                        "[%s] 📭 непрочитанных чатов: 0 — поллер не заходит в _process_chat "
                        "(новое в Авито должно помечать чат как unread; цикл #%d)",
                        client.name,
                        _poll_idle_cycles,
                    )

            if STALE_CHECK_EVERY and _poll_iteration % STALE_CHECK_EVERY == 0:
                try:
                    await _sync_missed_inbound_for_known_chats(
                        client, application, uid_self, client.name,
                    )
                except Exception as e:
                    log.error("[%s] Ошибка stale-check: %s", client.name, e, exc_info=True)

            if BROAD_CHATS_EVERY and _poll_iteration % BROAD_CHATS_EVERY == 0:
                try:
                    await _sync_missed_from_broad_chat_list(
                        client, application, uid_self, client.name,
                    )
                except Exception as e:
                    log.error("[%s] Ошибка broad-check: %s", client.name, e, exc_info=True)

        except Exception as e:
            log.error("[%s] Ошибка цикла: %s", client.name, e, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


# ─── Для использования из bot.py ─────────────────────────────────────────────

async def send_avito_reply(application, chat_id: str, text: str) -> bool:
    """
    Отправляет ручной ответ администратора в Авито-чат.
    Зеркалирует его в TG-группу отдельным сообщением.
    Используется callback-хендлерами в bot.py.
    """
    client: AvitoClient | None = application.bot_data.get("avito_client")
    if not client:
        log.error("send_avito_reply: avito_client не инициализирован")
        return False
    try:
        await client.send_message(chat_id, text)
        log.info("[Авито] ✅ Ручной ответ → %s", chat_id)
        return True
    except Exception as e:
        log.error("[Авито] ОШИБКА send_message %s: %s", chat_id, e)
        return False
