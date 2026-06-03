"""
bot.py — Telegram-бот для управления бронированиями площадки «Муза».

═══════════════════════════════════════════════════════════════════════
  ДЛЯ ВСЕХ В ЧАТЕ:
  • Авто-приветствие новых пользователей
  • Быстрые ответы на вопросы о цене и вместимости
  • Проверка даты по сообщению → свободна / занята
  • Парсинг Авито-сообщений → сохранение лидов в «Авито» лист

  ДЛЯ АДМИНИСТРАТОРА:
  /add            — добавить бронь (пошаговый диалог)
  /edit           — редактировать / отменить бронь
  /cancel_booking — снять бронь с даты
  /app            — открыть мини-приложение
  /stats          — статистика
  /help           — список команд
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import calendar as _cal
import html
import logging
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta
from enum import Enum, auto

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from sheets import (
    add_booking,
    add_lead,
    check_date,
    edit_booking,
    get_free_dates,
    remove_booking,
)
# get_all_bookings берём из SQLite — быстрее и консистентно с Mini App
import database
from database import get_all_bookings

# ─── Конфиг ───────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN               = os.getenv("TELEGRAM_BOT_TOKEN")
NOTIFY_USERNAME     = os.getenv("NOTIFY_USERNAME", "@rimskiymedved")
WEBAPP_URL          = os.getenv("WEBAPP_URL", "")   # https://yourdomain.com — URL мини-апп
SUPERADMIN_ID: int = int(os.getenv("SUPERADMIN_ID", "45028744"))
ADMIN_IDS: set[int] = {SUPERADMIN_ID}

# Сокращение для экранирования пользовательских данных в HTML
_e = html.escape

# ─── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
sheets_log = logging.getLogger("SHEETS")

# ─── Тексты ответов ───────────────────────────────────────────────────────────
GREETING_TEXT = (
    "Здравствуйте! Я помогу проверить свободные даты. "
    "Скажите, пожалуйста, дату, количество гостей и какое мероприятие."
)

FREE_TEXT = (
    "Пришлите, пожалуйста, Ваш номер телефона или напишите нам в Телеграм @muza_zal. "
    "Пришлём презентацию и условия 🤍"
)

BUSY_TEXT = "Это дата занята, к сожалению. Давайте рассмотрим другие даты?"

# ── FAQ — отредактируйте под реальные данные зала ────────────────────────────
FAQ_PRICE = (
    "💰 <b>Стоимость аренды зала:</b>\n\n"
    "Аренда от 15 000 ₽\n\n"
    "Точный расчёт зависит от даты, количества гостей и пакета услуг.\n"
    "📲 @muza_zal  ·  +79253579000"
)

FAQ_CAPACITY = (
    "👥 <b>Вместимость зала:</b>\n\n"
    "До 150 гостей\n\n"
    "Для уточнения по вашему мероприятию:\n"
    "📲 @muza_zal  ·  +79253579000"
)

# ─── Состояния диалогов ───────────────────────────────────────────────────────
class Add(Enum):
    DATE = auto(); CONFIRM_OVERWRITE = auto(); GUESTS = auto(); NAME = auto()
    PHONE = auto(); SOURCE = auto(); CLIENT_TYPE = auto(); COMMENT = auto()

class Cancel(Enum):
    DATE = auto()

class Edit(Enum):
    DATE = auto(); FIELD = auto(); VALUE = auto()

# ─── Клавиатуры ───────────────────────────────────────────────────────────────

# Подтверждение перезаписи (вместо ReplyKeyboard)
KB_OVERWRITE = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Перезаписать", callback_data="ow:yes"),
    InlineKeyboardButton("❌ Отмена",       callback_data="ow:no"),
]])

# Выбор поля для редактирования (вместо ReplyKeyboard)
KB_EDIT_INLINE = InlineKeyboardMarkup([
    [InlineKeyboardButton("Имя",       callback_data="ef:name"),
     InlineKeyboardButton("Гости",     callback_data="ef:guests")],
    [InlineKeyboardButton("Телефон",   callback_data="ef:phone"),
     InlineKeyboardButton("Источник",  callback_data="ef:source")],
    [InlineKeyboardButton("Тип",       callback_data="ef:client_type"),
     InlineKeyboardButton("Комментарий", callback_data="ef:comment")],
    [InlineKeyboardButton("🗑 Отменить бронь", callback_data="ef:cancel_booking"),
     InlineKeyboardButton("✅ Готово",          callback_data="ef:done")],
])

# ─── Месяцы / дни недели ──────────────────────────────────────────────────────
_MO = {
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

_WD = ["пн","вт","ср","чт","пт","сб","вс"]

_MONTHS_RU = {
    1:"Январь", 2:"Февраль", 3:"Март",
    4:"Апрель", 5:"Май",     6:"Июнь",
    7:"Июль",   8:"Август",  9:"Сентябрь",
    10:"Октябрь",11:"Ноябрь",12:"Декабрь",
}

# ─── Календарь (inline-клавиатура) ───────────────────────────────────────────

_CAL_DATE_FMT = "%d.%m.%Y"


def _build_month_picker(prefix: str) -> InlineKeyboardMarkup:
    """
    Шаг 1: выбор месяца. Показывает ближайшие 12 месяцев по 3 в строке.
    """
    today = date.today()
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(12):
        y = today.year + (today.month - 1 + i) // 12
        m = (today.month - 1 + i) % 12 + 1
        label = f"{_MONTHS_RU[m][:3]} {str(y)[2:]}"
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_month:{y}:{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _build_calendar(
    year: int,
    month: int,
    booked: set,
    free: set,
    prefix: str,          # "fcal" для /free, "acal" для /add
) -> InlineKeyboardMarkup:
    """
    Шаг 2: сетка дней выбранного месяца.
    prefix="fcal" → даты не кликабельны (только просмотр)
    prefix="acal" → свободные и незанятые даты кликабельны
    """
    today = date.today()
    rows: list[list[InlineKeyboardButton]] = [
        # Заголовок с месяцем и кнопкой «← Назад»
        [
            InlineKeyboardButton("← Назад", callback_data=f"{prefix}_back"),
            InlineKeyboardButton(f"{_MONTHS_RU[month]} {year}", callback_data=f"{prefix}_no"),
        ],
        # Дни недели
        [InlineKeyboardButton(d, callback_data=f"{prefix}_no")
         for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]],
    ]

    for week in _cal.monthcalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}_no"))
                continue
            d = date(year, month, day)
            if d < today:
                # В /add прошедшие даты кликабельны (запись задним числом)
                cb = f"acal_date:{d.isoformat()}" if prefix == "acal" else f"{prefix}_no"
                row.append(InlineKeyboardButton(f"·{day}·", callback_data=cb))
            elif d in booked:
                row.append(InlineKeyboardButton(f"❌{day}", callback_data=f"{prefix}_no"))
            elif d in free:
                if prefix == "fcal":
                    cb = f"fbook:{d.isoformat()}"   # клик → начать бронирование
                else:
                    cb = f"acal_date:{d.isoformat()}"
                row.append(InlineKeyboardButton(f"✅{day}", callback_data=cb))
            else:
                cb = f"{prefix}_no" if prefix == "fcal" else f"acal_date:{d.isoformat()}"
                row.append(InlineKeyboardButton(str(day), callback_data=cb))
        rows.append(row)

    return InlineKeyboardMarkup(rows)


async def _load_cal_data() -> tuple[set, set]:
    """Загружает занятые и свободные даты из таблицы для календаря."""
    booked = {b["date_obj"] for b in get_all_bookings()}
    free_list = get_free_dates(365)
    free_set = {datetime.strptime(d, _CAL_DATE_FMT).date() for d in free_list}
    return booked, free_set


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def parse_date(text: str) -> date | None:
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

    pat = r"\b(\d{1,2})\s+(" + "|".join(_MO) + r")(?:\s+(\d{4}))?\b"
    m = re.search(pat, t)
    if m:
        day, mon = int(m.group(1)), _MO[m.group(2)]
        yr = int(m.group(3)) if m.group(3) else today.year
        try:
            c = date(yr, mon, day)
            if not m.group(3) and c < today:
                c = date(yr + 1, mon, day)
            return c
        except ValueError:
            pass

    return None


def fmt_date(d: date) -> str:
    return f"{d.strftime('%d.%m.%Y')} ({_WD[d.weekday()]})"


def fmt_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith(("7", "8")):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return raw


def fmt_phone_link(raw: str) -> str:
    """Возвращает кликабельный номер телефона для HTML-режима Telegram."""
    if not raw or not raw.strip():
        return "—"
    display = fmt_phone(raw)
    digits = re.sub(r"\D", "", display)
    if len(digits) >= 10:
        return f'<a href="tel:+{digits}">{display}</a>'
    return display


def bank_suffix(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:             return "банкет"
    if n % 10 in (2,3,4) and n % 100 not in range(12,15): return "банкета"
    return "банкетов"


def _parse_guests(val: str) -> int:
    try:
        nums = re.findall(r"\d+", val or "")
        return int(nums[0]) if nums else 0
    except (ValueError, IndexError):
        return 0


def is_admin(update: Update) -> bool:
    uid = update.effective_user.id
    if uid == SUPERADMIN_ID:
        return True
    if database.is_allowed_user(uid):
        return True
    log.warning(f"Команда от НЕ-администратора: user_id={uid}")
    return False


def _parse_month_arg(args: list) -> tuple[int, int]:
    today = date.today()
    if not args:
        return today.year, today.month
    arg = " ".join(args).lower().strip()
    if arg.isdigit() and 1 <= int(arg) <= 12:
        return today.year, int(arg)
    m = re.match(r"(\d{1,2})[./](\d{4})", arg)
    if m:
        return int(m.group(2)), int(m.group(1))
    for name, month in _MO.items():
        if name in arg:
            yr_m = re.search(r"\b(20\d{2})\b", arg)
            year = int(yr_m.group(1)) if yr_m else today.year
            return year, month
    return today.year, today.month


# ─── Состояние чата ───────────────────────────────────────────────────────────

_seen_users: set[int] = set()   # для авто-приветствия

_PRICE_KEYWORDS    = {"цена","цены","стоимость","сколько стоит","прайс",
                      "расценки","сколько","почём","почем","аренда стоит"}
_CAPACITY_KEYWORDS = {"вместимость","вместить","вмещает","сколько человек",
                      "человек максимум","вместит"}


def _has_phone(text: str) -> bool:
    return bool(re.search(
        r'(\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}|\b\d{11}\b)',
        text
    ))


def _has_tg_nick(text: str) -> bool:
    return bool(re.search(r'@[a-zA-Z][a-zA-Z0-9_]{3,}', text))


def _is_price_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _PRICE_KEYWORDS)


def _is_capacity_question(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _CAPACITY_KEYWORDS)




# ══════════════════════════════════════════════════════════════════════════════
#  АВТО-ПРОВЕРКА ДАТ, ПРИВЕТСТВИЕ, FAQ
# ══════════════════════════════════════════════════════════════════════════════

async def auto_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = update.message.text or ""

    log.info(f"📨 {user.full_name} (id={user.id}): «{text[:80]}»")

    # ── В группе — только админы ──────────────────────────────────────────────
    if update.effective_chat.type in ("group", "supergroup"):
        if not is_admin(update):
            log.info("   ↳ Группа, не админ и не Авито-бот — пропускаем")
            return

    # ── Стандартная логика (лично или админ в группе) ────────────────────────
    tl = text.lower().strip()
    if tl in ("старт", "start", "начать", "привет", "hello"):
        log.info("   ↳ Ключевое слово: старт")
        await cmd_help(update, context)
        return
    if tl in ("команды", "помощь", "меню", "help"):
        log.info("   ↳ Ключевое слово: команды")
        await cmd_help(update, context)
        return
    if tl == "стат":
        log.info("   ↳ Ключевое слово: стат")
        await cmd_stats(update, context)
        return
    # ── Сначала проверяем дату — FAQ идёт после, чтобы «сколько стоит 15 июня?»
    #    дало оба ответа (статус даты + цена), а не только цену
    d = parse_date(text)
    if d is not None:
        log.info(f"   ↳ Дата: {d.strftime('%d.%m.%Y')}")
        try:
            result = check_date(d)
        except Exception as e:
            log.error(f"   ↳ ОШИБКА check_date: {e}", exc_info=True)
            return
        if result["found"]:
            log.info("   ↳ ЗАНЯТО")
            await update.message.reply_text(BUSY_TEXT)
        else:
            log.info("   ↳ СВОБОДНО → тегаем менеджера")
            await update.message.reply_text(FREE_TEXT)
            await update.message.reply_text(NOTIFY_USERNAME)
        return

    # ── Дата не найдена — проверяем FAQ и приветствие ────────────────────────
    if _is_price_question(text):
        log.info("   ↳ FAQ: цена")
        await update.message.reply_text(FAQ_PRICE, parse_mode="HTML")
        return
    if _is_capacity_question(text):
        log.info("   ↳ FAQ: вместимость")
        await update.message.reply_text(FAQ_CAPACITY, parse_mode="HTML")
        return

    if user.id not in _seen_users and not is_admin(update):
        _seen_users.add(user.id)
        log.info("   ↳ Новый пользователь — приветствие")
        await update.message.reply_text(GREETING_TEXT)
        return

    log.info("   ↳ Дата не найдена — пропускаем")


# ══════════════════════════════════════════════════════════════════════════════
#  КОМАНДЫ — ПРОСМОТР
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Проверяем ДО обновления — был ли ID уже привязан
    was_linked = database.is_allowed_user(user.id) if user else False

    if user and user.username:
        try:
            database.update_allowed_user_id(user.username, user.id)
        except Exception:
            pass

    is_adm = is_admin(update)

    # Первый вход: был неизвестен, стал админом → приветствие
    if is_adm and not was_linked and user.id != SUPERADMIN_ID:
        await update.message.reply_text(
            "🎉 <b>Добро пожаловать!</b>\n\n"
            "Теперь у вас есть доступ к системе бронирований «Муза».\n\n"
            "Нажмите /app чтобы открыть календарь.",
            parse_mode="HTML",
        )
        return

    if is_adm:
        text = (
            "<b>Команды бота</b>\n\n"
            "/app — 📅 открыть мини-приложение\n\n"
            "/add — добавить бронь\n"
            "/edit — редактировать бронь\n"
            "/cancel_booking — снять бронь\n"
            "/stats — статистика"
        )
    else:
        text = "<b>Команды бота</b>\n\nНапишите дату — я проверю, свободна ли она."
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /app — открыть мини-приложение «Муза» (только для администраторов).
    Кнопка типа WebApp открывает встроенный браузер Telegram.
    WEBAPP_URL задаётся в .env — это HTTPS-адрес сервера muza_api.
    """
    if not is_admin(update):
        await update.message.reply_text("🔒 Эта команда только для администраторов.")
        return

    if not WEBAPP_URL:
        await update.message.reply_text(
            "⚠️ <b>WEBAPP_URL не задан</b>\n\n"
            "Добавьте в <code>.env</code> на сервере:\n"
            "<code>WEBAPP_URL=https://ваш-домен.ru</code>\n\n"
            "После этого перезапустите бота: <code>docker compose restart muza_bot</code>",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📅 Открыть календарь",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])
    await update.message.reply_text(
        "📅 <b>Муза — управление бронированиями</b>\n\n"
        "Нажмите кнопку, чтобы открыть календарь.",
        parse_mode="HTML",
        reply_markup=kb,
    )




async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика: прошедшие / будущие / всего."""
    log.info(f"📌 /stats от {update.effective_user.full_name}")
    if not is_admin(update):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return

    try:
        bookings = get_all_bookings()
    except Exception as e:
        log.error(f"ОШИБКА get_all_bookings: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Не удалось загрузить таблицу.")
        return

    if not bookings:
        await update.message.reply_text("📭 Броней пока нет.")
        return

    past   = [b for b in bookings if not b["future"]]
    future = [b for b in bookings if     b["future"]]

    def _src_stats(group: list[dict]) -> tuple[int, int, Counter, Counter]:
        """Возвращает (кол-во, гости, source_count, source_guests)."""
        cnt    = len(group)
        guests = sum(_parse_guests(b["guests"]) for b in group)
        sc: Counter = Counter()
        sg: Counter = Counter()
        for b in group:
            src = b.get("source", "") or "Не указан"
            sc[src] += 1
            sg[src] += _parse_guests(b["guests"])
        return cnt, guests, sc, sg

    def _block(label: str, icon: str, group: list[dict]) -> list[str]:
        """Форматирует один блок статистики."""
        if not group:
            return [f"{icon} <b>{label}:</b> нет броней"]
        cnt, guests, sc, sg = _src_stats(group)
        g_text = f", {guests} гостей" if guests else ""
        block  = [f"{icon} <b>{label}: {cnt} {bank_suffix(cnt)}{g_text}</b>"]
        block.append("   📣 По каналам:")
        for src, c in sc.most_common():
            g = sg[src]
            block.append(
                f"   • {_e(src)}: <b>{c} {bank_suffix(c)}</b>"
                + (f", {g} гостей" if g else "")
            )
        return block

    lines = ["📊 <b>СТАТИСТИКА</b>\n"]
    lines += _block("ПРОШЕДШИЕ", "🕓", past)
    lines.append("")
    lines += _block("БУДУЩИЕ",   "📅", future)
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    # Итоговый блок (все вместе)
    total_cnt    = len(bookings)
    total_guests = sum(_parse_guests(b["guests"]) for b in bookings)
    g_total      = f", {total_guests} гостей" if total_guests else ""
    _, _, sc_all, sg_all = _src_stats(bookings)
    lines.append(f"🎉 <b>ВСЕГО: {total_cnt} {bank_suffix(total_cnt)}{g_total}</b>")
    lines.append("   📣 По каналам:")
    for src, c in sc_all.most_common():
        g = sg_all[src]
        lines.append(
            f"   • {_e(src)}: <b>{c} {bank_suffix(c)}</b>"
            + (f", {g} гостей" if g else "")
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")




# ══════════════════════════════════════════════════════════════════════════════
#  ДИАЛОГ: ДОБАВИТЬ БРОНЬ  (/add)
# ══════════════════════════════════════════════════════════════════════════════

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    log.info(f"📌 /add от {update.effective_user.full_name}")
    if not is_admin(update):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return ConversationHandler.END
    context.user_data.clear()
    kb = _build_month_picker(prefix="acal")
    await update.message.reply_text(
        "📋 <b>Добавление брони</b>\n\nВыберите месяц:",
        parse_mode="HTML", reply_markup=kb,
    )
    return Add.DATE


async def add_cal_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Заглушка для неактивных кнопок календаря (прошедшие даты, заголовки)."""
    await update.callback_query.answer()
    return Add.DATE


async def add_cal_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор месяца в /add — открывает сетку дней."""
    query = update.callback_query
    await query.answer()
    _, year_s, month_s = query.data.split(":")
    year, month = int(year_s), int(month_s)
    try:
        booked, free_set = await _load_cal_data()
    except Exception:
        await query.answer("⚠️ Ошибка загрузки", show_alert=True)
        return Add.DATE
    kb = _build_calendar(year, month, booked, free_set, prefix="acal")
    try:
        await query.edit_message_text(
            "📋 <b>Добавление брони</b>\n\nВыберите дату:\n"
            "✅ — свободно   ❌ — занято   ·· — прошедшее\n\n"
            "Или введите дату текстом (например: 20 мая)",
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass  # сообщение не изменилось — игнорируем
    return Add.DATE


async def add_cal_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «← Назад» в календаре /add — возврат к выбору месяца."""
    query = update.callback_query
    await query.answer()
    kb = _build_month_picker(prefix="acal")
    try:
        await query.edit_message_text(
            "📋 <b>Добавление брони</b>\n\nВыберите месяц:",
            parse_mode="HTML", reply_markup=kb,
        )
    except Exception:
        pass
    return Add.DATE


async def add_cal_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выбор даты через календарь в /add."""
    query = update.callback_query
    await query.answer()
    ds = query.data.replace("acal_date:", "")
    d = date.fromisoformat(ds)
    try:
        r = check_date(d)
        if r["found"]:
            await query.answer(f"⚠️ {fmt_date(d)} уже занята!", show_alert=True)
    except Exception:
        pass
    context.user_data["date"] = d
    await query.edit_message_text(
        f"📋 <b>Добавление брони</b>\n\n✅ Дата: <b>{fmt_date(d)}</b>",
        parse_mode="HTML",
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Сколько гостей?",
    )
    return Add.GUESTS


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text or ""
    d = parse_date(text)
    if d is None:
        await update.message.reply_text(
            "Не могу распознать дату. Попробуйте: 20 мая или 20.05.2026",
        )
        return Add.DATE
    try:
        r = check_date(d)
        if r["found"]:
            context.user_data["date"] = d
            await update.message.reply_text(
                f"⚠️ <b>{fmt_date(d)}</b> уже занята!\n\n"
                f"Клиент: {_e(r.get('name') or '—')}\n"
                "Перезаписать эту бронь?",
                parse_mode="HTML", reply_markup=KB_OVERWRITE,
            )
            return Add.CONFIRM_OVERWRITE
    except Exception as e:
        log.error(f"Ошибка check_date: {e}", exc_info=True)
    context.user_data["date"] = d
    await update.message.reply_text(
        f"✅ Дата: <b>{fmt_date(d)}</b>\n\nСколько гостей?",
        parse_mode="HTML",
    )
    return Add.GUESTS


async def add_overwrite_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "ow:no":
        await query.edit_message_text("Отменено.")
        context.user_data.clear()
        return ConversationHandler.END
    d = context.user_data.get("date")
    await query.edit_message_text(
        f"✅ Дата: <b>{fmt_date(d)}</b>\n\nСколько гостей?",
        parse_mode="HTML",
    )
    return Add.GUESTS


async def add_guests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    nums = re.findall(r"\d+", text)
    context.user_data["guests"] = nums[0] if nums else text
    await update.message.reply_text("Имя клиента:")
    return Add.NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = (update.message.text or "").strip()
    await update.message.reply_text("Телефон:")
    return Add.PHONE


async def add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    # Принимаем «—» / «нет» как пропуск; иначе проверяем что есть хотя бы 7 цифр
    skip_values = ("—", "-", "нет", "н/а", ".", "пропустить", "skip")
    digits = re.sub(r"\D", "", raw)
    if raw.lower() not in skip_values and len(digits) < 7:
        await update.message.reply_text(
            "Похоже, это не номер телефона. Введите номер (например +79001234567) "
            "или «—» чтобы пропустить:",
        )
        return Add.PHONE
    context.user_data["phone"] = "" if raw.lower() in skip_values else raw
    await update.message.reply_text("Источник рекламы (или «—»):")
    return Add.SOURCE


async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["source"] = (update.message.text or "").strip()
    await update.message.reply_text("Тип клиента (Прямой / Агентство):")
    return Add.CLIENT_TYPE


async def add_client_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["client_type"] = raw.replace("👤 ","").replace("🏢 ","")
    await update.message.reply_text("Комментарий (или «—» если нет):")
    return Add.COMMENT


async def add_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    comment = "" if raw in ("—","-","нет","н/а",".") else raw
    ud = context.user_data
    try:
        sheets_log.info(f"📊 add_booking: {ud['date'].strftime('%d.%m.%Y')} / {ud['name']}")
        add_booking(
            target=ud["date"], guests=ud["guests"], name=ud["name"],
            phone=ud["phone"], source=ud["source"],
            client_type=ud["client_type"], comment=comment,
            changed_by=update.effective_user.full_name,
        )
        sheets_log.info("   ↳ ✅ Бронь добавлена")
    except Exception as e:
        log.error(f"ОШИБКА add_booking: {e}", exc_info=True)
        await update.message.reply_text(
            f"⚠️ Ошибка при записи:\n<code>{_e(str(e))}</code>",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    d = ud["date"]
    await update.message.reply_text(
        f"✅ <b>Бронь добавлена!</b>\n\n"
        f"📅 {fmt_date(d)}\n"
        f"👥 Гостей: {_e(ud['guests'])}\n"
        f"👤 {_e(ud['name'])}  📞 {ud['phone']}\n"
        f"📣 {_e(ud['source'])} · {_e(ud['client_type'])}\n"
        f"💬 {_e(comment or '—')}",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  ДИАЛОГ: РЕДАКТИРОВАТЬ БРОНЬ  (/edit)
# ══════════════════════════════════════════════════════════════════════════════

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    log.info(f"📌 /edit от {update.effective_user.full_name}")
    if not is_admin(update):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "✏️ <b>Редактирование брони</b>\n\nНа какую дату? (например: 20 мая)",
        parse_mode="HTML",
    )
    return Edit.DATE


async def edit_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text or ""
    d = parse_date(text)
    if d is None:
        await update.message.reply_text("Не могу распознать дату. Попробуйте ещё раз.")
        return Edit.DATE
    try:
        r = check_date(d)
    except Exception as e:
        log.error(f"Ошибка check_date: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ошибка при обращении к таблице.")
        return ConversationHandler.END

    if not r["found"]:
        await update.message.reply_text(
            f"❌ Бронь на <b>{fmt_date(d)}</b> не найдена.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data["edit_date"] = d
    p = fmt_phone(r.get("phone","") or "")
    info = (
        f"📋 <b>{fmt_date(d)}</b>\n"
        f"👥 Гостей: {_e(r['guests'] or '—')}\n"
        f"👤 {_e(r['name'] or '—')}  📞 {p or '—'}\n"
        f"📣 {_e(r['source'] or '—')} · {_e(r.get('client_type','') or '—')}\n"
        f"💬 {_e(r['comment'] or '—')}\n\n"
        "Что изменить?"
    )
    await update.message.reply_text(info, parse_mode="HTML", reply_markup=KB_EDIT_INLINE)
    return Edit.FIELD


async def edit_field_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("edit_date")
    action = query.data.replace("ef:", "")

    if action == "done":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("Готово.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    if action == "cancel_booking":
        try:
            ok = remove_booking(d)
        except Exception as e:
            log.error(f"Ошибка remove_booking: {e}", exc_info=True)
            await query.message.reply_text("⚠️ Ошибка при отмене.", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
            return ConversationHandler.END
        msg = (f"✅ Бронь на <b>{fmt_date(d)}</b> отменена."
               if ok else "❌ Запись не найдена.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    field_names = {
        "name": "Имя", "guests": "Гости", "phone": "Телефон",
        "source": "Источник рекламы", "client_type": "Тип клиента", "comment": "Комментарий",
    }
    if action not in field_names:
        return Edit.FIELD

    context.user_data["edit_field_key"] = action
    context.user_data["edit_field"]     = field_names[action]
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(f"Новое значение для «{field_names[action]}»:")
    return Edit.VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value      = (update.message.text or "").strip()
    d          = context.user_data.get("edit_date")
    field_key  = context.user_data.get("edit_field_key")
    field_name = context.user_data.get("edit_field")

    if not d or not field_key:
        await update.message.reply_text("Ошибка. Начните заново /edit.", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return ConversationHandler.END

    try:
        ok = edit_booking(d, changed_by=update.effective_user.full_name, **{field_key: value})
    except Exception as e:
        log.error(f"Ошибка edit_booking: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ошибка при обновлении.\n\nЧто ещё изменить?", reply_markup=KB_EDIT_INLINE)
        return Edit.FIELD

    if ok:
        await update.message.reply_text(
            f"✅ <b>{_e(field_name)}</b> обновлено: {_e(value)}\n\nЧто ещё изменить?",
            parse_mode="HTML", reply_markup=KB_EDIT_INLINE,
        )
    else:
        await update.message.reply_text("❌ Не удалось обновить.\n\nЧто ещё изменить?", reply_markup=KB_EDIT_INLINE)
    return Edit.FIELD


# ══════════════════════════════════════════════════════════════════════════════
#  ДИАЛОГ: СНЯТЬ БРОНЬ  (/cancel_booking)
# ══════════════════════════════════════════════════════════════════════════════

async def cancel_booking_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    log.info(f"📌 /cancel_booking от {update.effective_user.full_name}")
    if not is_admin(update):
        await update.message.reply_text("⛔ Команда только для администратора.")
        return ConversationHandler.END
    await update.message.reply_text(
        "С какой даты снять бронь? (например: 20 мая)",
    )
    return Cancel.DATE


async def cancel_booking_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text or ""
    d = parse_date(text)
    if d is None:
        await update.message.reply_text("Не могу распознать дату. Попробуйте ещё раз.")
        return Cancel.DATE
    try:
        ok = remove_booking(d)
    except Exception as e:
        log.error(f"Ошибка remove_booking: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Ошибка при обращении к таблице.")
        return ConversationHandler.END
    if ok:
        await update.message.reply_text(
            f"✅ Бронь на <b>{fmt_date(d)}</b> снята. Дата возвращена в свободные.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"❓ Дата <b>{fmt_date(d)}</b> не найдена.", parse_mode="HTML")
    return ConversationHandler.END


async def _cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  АВИТО — ОТВЕТЫ ЧЕРЕЗ TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def avito_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатия кнопок под Авито-карточкой.

    Паттерны:
      av_free:{chat_id} → отправить «дата свободна» в Авито
      av_busy:{chat_id} → отправить «дата занята» в Авито
      av_hint:{chat_id} → подсказка как ответить вручную
    """
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return

    data     = query.data             # "av_free:CHAT_ID" и т.д.
    action, _, chat_id = data.partition(":")

    try:
        from avito_poll import send_avito_reply, REPLY_FREE, REPLY_BUSY
    except ImportError:
        await query.answer("⚠️ Авито не подключён.", show_alert=True)
        return

    if action == "av_hint":
        await query.answer(
            "✏️ Ответьте на это сообщение в Telegram — "
            "ваш текст автоматически уйдёт клиенту в Авито.",
            show_alert=True,
        )
        return

    if action == "av_free":
        reply_text = REPLY_FREE
        label      = "✅ Дата свободна"
    elif action == "av_busy":
        reply_text = REPLY_BUSY
        label      = "❌ Дата занята"
    else:
        return

    ok = await send_avito_reply(context.application, chat_id, reply_text)
    if ok:
        # Убираем кнопки с карточки и добавляем подпись об отправке
        sender = update.effective_user.full_name
        try:
            original  = query.message.text or ""
            new_text  = original.rstrip() + f"\n\n<i>— {_e(sender)} отправил: {label}</i>"
            await query.edit_message_text(
                new_text, parse_mode="HTML", reply_markup=None,
            )
        except Exception:
            pass
        await query.message.reply_text(f"✅ Ответ отправлен в Авито: {label}")
    else:
        await query.answer("⚠️ Ошибка при отправке.", show_alert=True)


async def handle_tg_reply_to_avito(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Запускается для ВСЕХ текстовых сообщений (group=1).
    Если сообщение является реплаем на Авито-карточку — отправляет его в Авито.
    Иначе — ничего не делает (группа 0 обработает стандартно).
    """
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    tg_to_avito: dict[int, str] = context.bot_data.get("tg_to_avito", {})
    chat_id = tg_to_avito.get(msg.reply_to_message.message_id)
    if not chat_id:
        return   # не Авито-карточка — пропускаем

    if not is_admin(update):
        return

    text = (msg.text or "").strip()
    if not text:
        return

    try:
        from avito_poll import send_avito_reply
    except ImportError:
        return

    ok = await send_avito_reply(context.application, chat_id, text)
    if not ok:
        await msg.reply_text("⚠️ Не удалось отправить сообщение в Авито.")


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env!")

    log.info("=" * 60)
    log.info("  Запуск бота бронирования «Муза»")
    log.info("=" * 60)
    log.info(f"  Суперадмин     : {SUPERADMIN_ID}")
    try:
        _extra_admins = database.get_allowed_users()
        if _extra_admins:
            for _u in _extra_admins:
                _tid = _u['telegram_id'] or '⏳ не писал боту'
                log.info(f"  Администратор  : @{_u['username']} (tg_id={_tid})")
        else:
            log.info("  Администраторы : только суперадмин")
    except Exception:
        log.info("  Администраторы : БД недоступна при старте")
    log.info(f"  SPREADSHEET_ID : {os.getenv('SPREADSHEET_ID','⚠️  НЕ ЗАДАН')}")
    log.info(f"  credentials    : {'✅' if os.path.exists(os.getenv('GOOGLE_CREDENTIALS_PATH','credentials.json')) else '❌ НЕ НАЙДЕН'}")

    # Авито поллер — запускается если заданы ключи в .env
    _avito_client = None
    avito_cid     = os.getenv("AVITO_CLIENT_ID", "")
    avito_csecret = os.getenv("AVITO_CLIENT_SECRET", "")
    if avito_cid and avito_csecret:
        try:
            from avito      import AvitoClient
            from avito_poll import avito_polling_loop
            _avito_client = AvitoClient(
                client_id     = avito_cid,
                client_secret = avito_csecret,
                name          = os.getenv("AVITO_ACCOUNT_NAME", "Муза"),
            )
            log.info("  Авито поллер  : ✅ будет запущен")
        except ImportError as e:
            log.warning("  Авито поллер  : ⚠️  avito.py / avito_poll.py не найден: %s", e)
    else:
        log.info("  Авито поллер  : ⏭  AVITO_CLIENT_ID не задан, пропускаем")
    log.info("=" * 60)

    async def post_init(application) -> None:
        """Инициализируем SQLite и запускаем Авито поллер."""
        # ── SQLite: инициализация схемы + синхронизация из Sheets ────────────
        try:
            from database import init_db, sync_from_sheets
            init_db()
            log.info("  SQLite         : ✅ схема готова")
            try:
                sync_from_sheets()
                log.info("  SQLite sync    : ✅ данные из Sheets загружены")
            except Exception as _sync_err:
                log.warning("  SQLite sync    : ⚠️  %s — работаем из кеша", _sync_err)
        except ImportError:
            log.info("  SQLite         : ⏭  database.py не найден, пропускаем")
        except Exception as _db_err:
            log.warning("  SQLite         : ⚠️  init_db ошибка: %s", _db_err)

        # ── Авито поллер ──────────────────────────────────────────────────────
        if _avito_client:
            asyncio.create_task(avito_polling_loop(_avito_client, application))

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
        ],
        states={
            Add.DATE: [
                CallbackQueryHandler(add_cal_month, pattern=r"^acal_month:"),
                CallbackQueryHandler(add_cal_back,  pattern=r"^acal_back$"),
                CallbackQueryHandler(add_cal_date,  pattern=r"^acal_date:"),
                CallbackQueryHandler(add_cal_no, pattern=r"^acal_no$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_date),
            ],
            Add.CONFIRM_OVERWRITE: [
                CallbackQueryHandler(add_overwrite_cb, pattern=r"^ow:"),
            ],
            Add.GUESTS:      [MessageHandler(filters.TEXT & ~filters.COMMAND, add_guests)],
            Add.NAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            Add.PHONE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone)],
            Add.SOURCE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, add_source)],
            Add.CLIENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_type)],
            Add.COMMENT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_comment)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_conv)],
        per_chat=False, per_user=True, per_message=False,
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_start),
        ],
        states={
            Edit.DATE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_date)],
            Edit.FIELD: [CallbackQueryHandler(edit_field_cb, pattern=r"^ef:")],
            Edit.VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_conv)],
        per_chat=False, per_user=True, per_message=False,
    )

    cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel_booking", cancel_booking_start)],
        states={
            Cancel.DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_booking_date)],
        },
        fallbacks=[CommandHandler("cancel", _cancel_conv)],
        per_chat=False, per_user=True, per_message=False,
    )

    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("start",    cmd_help))
    app.add_handler(CommandHandler("app",      cmd_app))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CallbackQueryHandler(avito_callback, pattern=r"^av_"))

    # group=0: стандартная логика
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_check))
    # group=1: перехватывает реплаи на Авито-карточки (работает параллельно с group=0)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tg_reply_to_avito),
        group=1,
    )

    log.info("Бот запущен. Жду сообщения...\n")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
