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
  — Каждое НОВОЕ сообщение клиента → новое TG-сообщение
  — Каждый авто-ответ бота → новое TG-сообщение
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
from sheets import add_lead, check_date

log = logging.getLogger("AVITO_POLL")
_e = html.escape

# ─── Конфиг ──────────────────────────────────────────────────────────────────

POLL_INTERVAL      = int(os.getenv("AVITO_POLL_INTERVAL", "30"))
AVITO_NOTIFY_GROUP = int(os.getenv("AVITO_NOTIFY_GROUP_ID", "0"))
ADMIN_TG_USERNAME  = os.getenv("NOTIFY_USERNAME", "")

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
    msg_text   = msg.get("content", {}).get("text", "").strip()
    ts_str     = _fmt_ts(msg.get("created", 0))
    edit_mark  = "  <i>✏️ изменено</i>" if edited else ""
    current_id = msg.get("id", "")

    lines = []

    # ── Новое сообщение — вверху, имя жирное, без даты ───────────────────────
    lines.append(f"<b>Авито · {_e(account_name)}</b>")
    lines.append(f"<b>{_e(buyer_name)}:</b> {_e(msg_text[:400])}{edit_mark}")

    # ── История последних 4 сообщений — ниже, дд.мм чч:мм Имя: текст ────────
    if all_messages:
        history = [
            m for m in all_messages
            if m.get("type") == "text" and m.get("id") != current_id
        ]
        history.sort(key=lambda m: m.get("created", 0))
        history = history[-4:]

        if history:
            lines.append("")
            for m in history:
                ts  = _fmt_ts(m.get("created", 0))
                out = m.get("direction") == "out"
                nm  = account_name if out else buyer_name
                txt = m.get("content", {}).get("text", "").strip()
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
    if not AVITO_NOTIFY_GROUP:
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

    log.info("   [dbg] messages=%d, chat=%s", len(messages), chat_id[:16])
    if not messages:
        log.info("   [dbg] → нет сообщений, пропуск")
        return

    incoming = [
        m for m in messages
        if m.get("direction") == "in" and m.get("type") == "text"
    ]
    log.info("   [dbg] incoming=%d, типы=%s",
             len(incoming),
             [m.get("type") for m in messages[:5]])
    if not incoming:
        log.info("   [dbg] → нет входящих текстовых, пропуск")
        try: await client.mark_read(chat_id)
        except: pass
        return

    # Берём самое новое входящее (API отдаёт newest-first, но берём max на всякий случай)
    last_in  = max(incoming, key=lambda m: m.get("created", 0))
    msg_id   = last_in.get("id", "")
    msg_ts   = last_in.get("created", 0)
    msg_text = last_in.get("content", {}).get("text", "").strip()

    log.info("   [dbg] last_in ts=%s _start_ts=%s diff=%+.0f сек",
             msg_ts, int(_start_ts), msg_ts - _start_ts)

    # Пропускаем сообщения до запуска бота
    if msg_ts < _start_ts:
        log.info("   [dbg] → сообщение старее запуска, пропуск")
        if _last_handled.get(chat_id) != msg_id:
            _last_handled[chat_id] = msg_id
            _msg_content[msg_id]   = msg_text
            try: await client.mark_read(chat_id)
            except: pass
        return

    buyer      = _find_buyer(chat_info.get("users", []), uid_self)
    buyer_name = buyer.get("name", "")

    # ══ Определяем: новое сообщение или редактирование ══════════════════════

    if _last_handled.get(chat_id) == msg_id:
        # Тот же msg_id — проверяем, изменился ли текст (редактирование)
        if _msg_content.get(msg_id) == msg_text:
            return  # Ничего нового
        # ── Клиент отредактировал сообщение ─────────────────────────────────
        log.info("[%s] ✏️ Редактирование: %s | «%s»", account_name, buyer_name or "?", msg_text[:80])
        _msg_content[msg_id] = msg_text

        edited_card = _format_client_msg(
            chat_info, last_in, uid_self, account_name, messages,
            include_meta=True, edited=True,
        )
        await _update_tg_msg(application, msg_id, edited_card, keyboard=_build_keyboard(chat_id))
        try: await client.mark_read(chat_id)
        except: pass
        return

    # ══ Новое входящее сообщение ═════════════════════════════════════════════

    log.info("[%s] 💬 %s | «%s»", account_name, buyer_name or "?", msg_text[:80])
    _msg_content[msg_id] = msg_text

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
                log.error("   ↳ check_date: %s", e)
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
                await _check_dates_reply(dates)
            # Всё остальное — молчим, менеджер уже ведёт диалог

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
            log.info("   ↳ Найдено дат: %d", len(dates))
            await _check_dates_reply(dates)
        else:
            # Повторное сообщение без даты — спрашиваем один раз
            if chat_id not in _asked_date:
                log.info("   ↳ Повторное без даты — спрашиваем дату")
                auto_replies.append(REPLY_ASK_DATE)
                _asked_date.add(chat_id)
            else:
                log.info("   ↳ Повторное без даты — уже спрашивали, ожидаем")

    # ══ Отправляем авто-ответы в Авито + зеркалируем в TG ═══════════════════

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
    global _start_ts
    _start_ts = time.time()

    _load_state()

    application.bot_data["avito_client"] = client
    application.bot_data.setdefault("tg_to_avito", {})

    log.info("[%s] 🚀 Поллер запущен (интервал %d сек)", client.name, POLL_INTERVAL)
    if not AVITO_NOTIFY_GROUP:
        log.warning("[%s] ⚠️  AVITO_NOTIFY_GROUP_ID не задан", client.name)

    try:
        uid_self = await client.get_user_id()
    except Exception as e:
        log.error("[%s] Ошибка user_id: %s — поллер не запущен", client.name, e)
        return

    _net_fails = 0  # счётчик последовательных сетевых ошибок

    while True:
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

            if chats:
                log.info("[%s] 📬 %d непрочитанных", client.name, len(chats))
                for chat in chats:
                    try:
                        await _process_chat(client, chat, application, uid_self, client.name)
                    except Exception as e:
                        log.error("[%s] Ошибка чата %s: %s",
                                  client.name, chat.get("id","?"), e, exc_info=True)
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
