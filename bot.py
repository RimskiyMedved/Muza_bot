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
import json as _json
import logging
import os
import re
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from enum import Enum, auto

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
from config import (
    SUPERADMIN_ID, NOTIFY_USERNAME, WEBAPP_URL, TELEGRAM_BOT_TOKEN,
    AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_ACCOUNT_NAME,
    GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID,
)
from utils import (
    MONTHS,
    MONTHS_RU,
    WEEKDAYS_SHORT,
    fmt_phone,
    has_phone,
    has_tg_nick,
    parse_date,
)

# ─── Конфиг ───────────────────────────────────────────────────────────────────
TOKEN     = TELEGRAM_BOT_TOKEN   # локальный псевдоним для читаемости
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

# ─── Groq Whisper: голосовые команды ─────────────────────────────────────────
_GROQ_KEY    = os.getenv("GROQ_API_KEY", "")
_groq_client = None

if _GROQ_KEY:
    try:
        from groq import Groq as _Groq
        _groq_client = _Groq(api_key=_GROQ_KEY)
    except ImportError:
        log.warning("Groq: пакет не установлен — голосовые команды недоступны")


async def _transcribe_voice(file_path: str) -> str:
    """Транскрибирует аудио через Groq Whisper."""
    loop = asyncio.get_event_loop()
    def _do():
        with open(file_path, "rb") as f:
            return _groq_client.audio.transcriptions.create(
                file=(os.path.basename(file_path), f),
                model="whisper-large-v3-turbo",
                language="ru",
                response_format="text",
            )
    result = await loop.run_in_executor(None, _do)
    return str(result).strip()


# Читаемые названия полей для diff-отображения
_FIELD_LABELS = {
    "name":              "Имя",
    "phone":             "Телефон",
    "source":            "Источник",
    "guests":            "Гости",
    "comment":           "Комментарий",
    "contract_date":     "Дата договора",
    "revenue_rent":      "Аренда",
    "revenue_menu":      "Меню",
    "paid_advance":      "Аванс",
    "paid_advance_date": "Дата аванса",
    "paid_rent":         "Оплата аренды",
    "paid_rent_date":    "Дата оплаты аренды",
    "paid_final":        "Итоговая оплата",
    "paid_final_date":   "Дата итоговой",
    "staff_waiters":     "Официанты",
    "staff_cooks":       "Повара",
    "cost_laundry":      "Прачка",
    "cost_purchase":     "Закупка",
    "cost_extra":        "Доп. расходы",
    "new_date":          "Новая дата брони",
}
_MONEY_FIELDS = {
    "cost_laundry", "cost_purchase", "cost_extra",
    "revenue_rent", "revenue_menu",
    "paid_advance", "paid_rent", "paid_final",
}


def _fmt_field_val(field: str, value) -> str:
    if value is None or value == "" or value == 0:
        return "—"
    if field in _MONEY_FIELDS:
        return f"{int(float(value)):,} ₽".replace(",", " ")
    return str(value)

def _booking_by_date(dt_str: str) -> dict | None:
    for b in database.get_all_bookings():
        if b["date"] == dt_str:
            return b
    return None


def _merge_edit_fields(parsed: dict) -> dict:
    """Для edit: переносит name/guests/phone/source с верхнего уровня в fields.

    Маленькая LLM иногда кладёт эти поля наверх (как у add), а не в fields —
    из-за чего правка «занеси имя и гостей» не распознавалась. Подстраховка кодом.
    """
    fields = dict(parsed.get("fields") or {})
    for k in ("name", "guests", "phone", "source"):
        v = parsed.get(k)
        if v not in (None, "", []) and k not in fields:
            fields[k] = str(v) if k == "guests" else v
    return fields


# Текст «что я умею» — общий для /help, /start и голосового вопроса «что ты умеешь?»
_HELP_TEXT = (
    "🎙 <b>Муза — что я умею</b>\n\n"
    "Веду бронирования зала. Можно голосом, командами или через приложение.\n\n"
    "📅 <b>Приложение</b>\n"
    "/app — календарь, список броней, финансы и статистика.\n\n"
    "🎙 <b>Голосом</b> (просто надиктуй):\n"
    "• «Добавь Рамазан, 50 гостей, 31 декабря» — новая бронь\n"
    "• «Покажи бронь на 10 июля» — карточка брони\n"
    "• «Удали бронь на 20 июля» — отменить\n"
    "• «Бронь 15 июня: прачка 20 тысяч, официанты 2» — изменить поля\n"
    "• «Отредактируй бронь на 31 декабря» — спрошу, что поменять\n"
    "• «Перенеси бронь с 5 на 8 августа» — сменить дату\n\n"
    "⌨️ <b>Командами</b>\n"
    "/add — добавить бронь\n"
    "/edit — редактировать\n"
    "/cancel_booking — снять бронь\n"
    "/stats — статистика\n"
    "/help — это сообщение\n\n"
    "В правках можно менять что угодно: имя, гостей, телефон, источник, "
    "оплаты, расходы, персонал."
)
_VOICE_HELP = {
    "что ты умеешь", "что умеешь", "что ты можешь", "что можешь",
    "что я умею", "помощь", "справка", "хелп", "help",
}


async def _parse_voice_command(text: str) -> dict:
    """Извлекает из транскрипции все поля брони через Groq LLM."""
    today = date.today()
    y = today.year
    system = (
        f"Сегодня {today.strftime('%d.%m.%Y')}. Управляешь бронированиями зала «Муза».\n"
        "Из текста извлеки действие и верни ТОЛЬКО JSON:\n"
        '{"action":"add"/"delete"/"edit"/"show",'
        '"date":"ДД.ММ.ГГГГ или null",'
        '"name":null,"guests":null,"phone":null,"source":null,'
        '"fields":{}}\n\n'
        "action:\n"
        "  add    — добавить/записать/забронировать\n"
        "  delete — удалить/отменить бронь\n"
        "  edit   — изменить/внести изменения/редактировать/обнови/поставь/прачка/официанты/расходы/закупка\n"
        "  show   — покажи/что на/карточка/инфо о брони\n\n"
        "ВАЖНО: если слышишь «внеси изменения», «внеси данные», «обнови», «поставь» — это ВСЕГДА edit.\n"
        "fields (только для edit, только упомянутые поля):\n"
        "  name — имя клиента\n"
        "  phone — телефон\n"
        "  source — источник рекламы\n"
        "  guests — кол-во гостей (строка)\n"
        "  comment — комментарий\n"
        "  contract_date — дата договора (ДД.ММ.ГГГГ)\n"
        "  revenue_rent, revenue_menu — суммы аренды и меню\n"
        "  paid_advance — аванс, paid_advance_date — дата аванса (ДД.ММ.ГГГГ)\n"
        "  paid_rent — оплата аренды, paid_rent_date — дата оплаты (ДД.ММ.ГГГГ)\n"
        "  paid_final — итоговая, paid_final_date — дата итоговой (ДД.ММ.ГГГГ)\n"
        "  staff_waiters, staff_cooks — целые числа\n"
        "  cost_laundry, cost_purchase, cost_extra — расходы\n"
        "  new_date — новая дата брони (ДД.ММ.ГГГГ) если просят перенести/сменить дату\n\n"
        "Суммы: «20 тысяч»=20000, «5к»=5000, «полтора»=1500\n\n"
        "Примеры:\n"
        f'"прачка 20 тысяч официанты 2 на 15 июня" → {{"action":"edit","date":"15.06.{y}","name":null,"guests":null,"phone":null,"source":null,"fields":{{"cost_laundry":20000,"staff_waiters":2}}}}\n'
        f'"отредактируй бронь 31 декабря, имя Рамазан и 50 гостей" → {{"action":"edit","date":"31.12.{y}","name":null,"guests":null,"phone":null,"source":null,"fields":{{"name":"Рамазан","guests":"50"}}}}\n'
        f'"покажи бронь 10 июля" → {{"action":"show","date":"10.07.{y}","name":null,"guests":null,"phone":null,"source":null,"fields":{{}}}}\n'
        f'"добавь Иван 50 гостей 15 июня" → {{"action":"add","date":"15.06.{y}","name":"Иван","guests":50,"phone":null,"source":null,"fields":{{}}}}'
    )
    loop = asyncio.get_event_loop()
    def _do():
        return _groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": text},
            ],
            temperature=0,
            max_tokens=200,
        )
    resp = await loop.run_in_executor(None, _do)
    raw = resp.choices[0].message.content.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError(f"LLM вернул неожиданный ответ: {raw}")
    return _json.loads(m.group())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голосовое управление бронями — только для администраторов."""
    user = update.effective_user
    if not user or user.id not in ADMIN_IDS:
        return
    if not _groq_client:
        await update.message.reply_text("⚠️ Голосовые команды не настроены (GROQ_API_KEY).")
        return

    status_msg = await update.message.reply_text("🎙 Распознаю…")
    tmp_path = None
    try:
        voice = update.message.voice or update.message.audio
        tg_file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        import os as _os
        audio_size = _os.path.getsize(tmp_path) if tmp_path else 0
        audio_sec  = audio_size / 16000          # грубая оценка для OGG
        text = await _transcribe_voice(tmp_path)
        log.info("🎙 Транскрипция: %s", text)
        database.log_voice_usage(audio_seconds=audio_sec, llm_call=False)

        # Персональное приветствие голосом
        _VOICE_GREETINGS = {"привет", "хай", "хеллоу", "hello", "hi",
                            "здравствуй", "здравствуйте", "добрый день",
                            "добрый вечер", "доброе утро", "ку"}
        if text.lower().strip().rstrip("!.") in _VOICE_GREETINGS:
            first = update.effective_user.first_name or "друг"
            await status_msg.edit_text(f"Привет, {first}! 👋")
            return

        # Вопрос «что ты умеешь?» голосом → справка
        if text.lower().strip().rstrip("!.?") in _VOICE_HELP:
            await status_msg.edit_text(_HELP_TEXT, parse_mode="HTML")
            return

        # ── Доуточнение правок: ждём, какие поля менять у ранее названной брони ──
        pending_edit = context.user_data.get("pending_edit_date")
        if pending_edit:
            # Текст пользователя — это перечисление полей; подставляем сохранённую дату
            augmented = f"{text} на {pending_edit}"
            parsed_e  = await _parse_voice_command(augmented)
            fields_e  = _merge_edit_fields(parsed_e)
            booking   = _booking_by_date(pending_edit)
            if not booking:
                context.user_data.pop("pending_edit_date", None)
                context.user_data.pop("pending_edit_name", None)
                await status_msg.edit_text(
                    f"❌ Бронь на {_e(pending_edit)} не найдена.", parse_mode="HTML")
                return
            if fields_e:
                context.user_data.pop("pending_edit_date", None)
                context.user_data.pop("pending_edit_name", None)
                diff_lines = []
                for k, new_val in fields_e.items():
                    label   = _FIELD_LABELS.get(k, k)
                    old_val = _fmt_field_val(k, booking.get(k))
                    new_fmt = _fmt_field_val(k, new_val)
                    diff_lines.append(f"  {label}: {old_val} → <b>{new_fmt}</b>")
                context.user_data["voice_pending"] = {
                    "action": "edit", "date": pending_edit,
                    "fields": fields_e,
                    "booking_name": booking.get("name", ""),
                    "transcript": text,
                }
                await status_msg.edit_text(
                    f"🎙 «{_e(text)}»\n\n"
                    f"✏️ <b>Изменить бронь?</b>\n"
                    f"📅 {_e(pending_edit)} — {_e(booking.get('name',''))}\n\n"
                    + "\n".join(diff_lines),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Да",  callback_data="voice:confirm"),
                        InlineKeyboardButton("❌ Нет", callback_data="voice:cancel"),
                    ]]),
                )
                return
            # Поля не распознаны — переспрашиваем, не сбрасывая режим
            await status_msg.edit_text(
                f"🎙 «{_e(text)}»\n\n"
                f"❓ Не понял, что изменить в брони на {_e(pending_edit)}.\n"
                "Скажи ещё раз, например: «аванс 50 тысяч» или «телефон 9161234567».",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="voice:edit_cancel"),
                ]]),
            )
            return

        parsed  = await _parse_voice_command(text)
        action  = parsed.get("action")
        name    = parsed.get("name")    or ""
        dt_str  = parsed.get("date")
        guests  = parsed.get("guests")
        phone   = parsed.get("phone")   or ""
        source  = parsed.get("source")  or ""

        if not action or not dt_str:
            await status_msg.edit_text(
                f"🎙 Распознал: «{_e(text)}»\n\n"
                "❓ Не понял команду. Попробуй:\n"
                "• «Добавь Иван 50 гостей 15 июня»\n"
                "• «Прачка 20 тысяч официанты 2 на 15 июня»\n"
                "• «Покажи бронь 10 июля»\n"
                "• «Удали бронь на 20 июля»",
                parse_mode="HTML",
            )
            return

        kb_confirm = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да",  callback_data="voice:confirm"),
            InlineKeyboardButton("❌ Нет", callback_data="voice:cancel"),
        ]])

        # ── SHOW: красивая карточка брони ────────────────────────────────────
        if action == "show":
            booking = _booking_by_date(dt_str)
            if not booking:
                await status_msg.edit_text(f"🎙 «{_e(text)}»\n\n❌ Бронь на {_e(dt_str)} не найдена.", parse_mode="HTML")
                return
            await status_msg.edit_text("🎙 Генерирую карточку…")
            try:
                from card_gen import generate_booking_card
                card_bytes = generate_booking_card(booking)
            except Exception:
                card_bytes = None

            phone = booking.get("phone") or ""
            kb_call = InlineKeyboardMarkup([[
                InlineKeyboardButton("📞 Позвонить", url=f"tel:{phone}"),
            ]]) if phone else None

            if card_bytes:
                import io as _io
                await update.message.reply_photo(
                    photo=_io.BytesIO(card_bytes),
                    caption=f"📅 {_e(dt_str)} — {_e(booking.get('name',''))}",
                    reply_markup=kb_call,
                    parse_mode="HTML",
                )
                await status_msg.delete()
            else:
                # Фолбэк — текст если Pillow не установлен
                b = booking
                lines = [f"📅 <b>{_e(dt_str)}</b> — {_e(b.get('weekday',''))}",
                         f"👤 {_e(b.get('name','—'))}"]
                if b.get("guests"): lines.append(f"👥 {b['guests']} гостей")
                if phone:           lines.append(f"📞 {_e(phone)}")
                await status_msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_call)
            return

        # ── EDIT: изменить поля брони ────────────────────────────────────────
        if action == "edit":
            fields = _merge_edit_fields(parsed)
            booking = _booking_by_date(dt_str)
            if not booking:
                await status_msg.edit_text(f"🎙 «{_e(text)}»\n\n❌ Бронь на {_e(dt_str)} не найдена.", parse_mode="HTML")
                return
            if not fields:
                # Дата есть, бронь есть, но поля не названы — переходим в режим доуточнения
                context.user_data["pending_edit_date"] = dt_str
                context.user_data["pending_edit_name"] = booking.get("name", "")
                await status_msg.edit_text(
                    f"🎙 «{_e(text)}»\n\n"
                    f"✏️ Бронь на <b>{_e(dt_str)}</b> — {_e(booking.get('name',''))}.\n"
                    "Что изменить? Скажи, например: «аванс 50 тысяч», «официанты 3», «телефон 9161234567».",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Отмена", callback_data="voice:edit_cancel"),
                    ]]),
                )
                return

            diff_lines = []
            for k, new_val in fields.items():
                label   = _FIELD_LABELS.get(k, k)
                old_val = _fmt_field_val(k, booking.get(k))
                new_fmt = _fmt_field_val(k, new_val)
                diff_lines.append(f"  {label}: {old_val} → <b>{new_fmt}</b>")

            context.user_data["voice_pending"] = {
                "action": "edit", "date": dt_str,
                "fields": fields,
                "booking_name": booking.get("name", ""),
                "transcript": text,
            }
            await status_msg.edit_text(
                f"🎙 «{_e(text)}»\n\n"
                f"✏️ <b>Изменить бронь?</b>\n"
                f"📅 {_e(dt_str)} — {_e(booking.get('name',''))}\n\n"
                + "\n".join(diff_lines),
                parse_mode="HTML",
                reply_markup=kb_confirm,
            )
            return

        # ── ADD / DELETE ──────────────────────────────────────────────────────
        context.user_data["voice_pending"] = {
            "action": action, "name": name, "date": dt_str,
            "guests": str(guests) if guests else "",
            "phone": phone, "source": source, "transcript": text,
        }

        action_label = "➕ Добавить бронь" if action == "add" else "🗑 Удалить бронь"
        lines = [f"📅 {_e(dt_str)}"]
        if name:    lines.append(f"👤 {_e(name)}")
        if guests:  lines.append(f"👥 {guests} гостей")
        if phone:   lines.append(f"📞 {_e(phone)}")
        if source:  lines.append(f"📢 {_e(source)}")

        await status_msg.edit_text(
            f"🎙 «{_e(text)}»\n\n<b>{action_label}?</b>\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb_confirm,
        )

    except Exception as exc:
        log.exception("Ошибка голосовой команды")
        await status_msg.edit_text(f"❌ Ошибка: {_e(str(exc))}", parse_mode="HTML")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def voice_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение/отмена голосовой команды."""
    q = update.callback_query
    await q.answer()

    if q.data in ("voice:cancel", "voice:edit_cancel"):
        context.user_data.pop("voice_pending", None)
        context.user_data.pop("pending_edit_date", None)
        context.user_data.pop("pending_edit_name", None)
        await q.edit_message_text("❌ Отменено.")
        return

    pending = context.user_data.pop("voice_pending", None)
    if not pending:
        await q.edit_message_text("❌ Данные устарели — отправь голосовое заново.")
        return

    action  = pending["action"]
    dt_str  = pending["date"]
    name    = pending.get("name", "")
    guests  = pending.get("guests", "")
    phone   = pending.get("phone", "")
    source  = pending.get("source") or ""
    transcript = pending.get("transcript", "")
    uname   = q.from_user.username or str(q.from_user.id)

    try:
        d = datetime.strptime(dt_str, "%d.%m.%Y").date()
    except ValueError:
        await q.edit_message_text(f"❌ Неверный формат даты: {_e(dt_str)}", parse_mode="HTML")
        return

    try:
        if action == "add":
            # Предупреждение если бронь уже существует
            if not pending.get("overwrite_ok"):
                existing = _booking_by_date(dt_str)
                if existing:
                    context.user_data["voice_pending"] = {**pending, "overwrite_ok": True}
                    await q.edit_message_text(
                        f"⚠️ На <b>{_e(dt_str)}</b> уже есть бронь — <b>{_e(existing.get('name',''))}</b>!\n\n"
                        f"Перезаписать?",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Да, перезаписать", callback_data="voice:confirm"),
                            InlineKeyboardButton("❌ Отмена",           callback_data="voice:cancel"),
                        ]]),
                    )
                    return
            weekday = _WD[d.weekday()]
            from sheets import add_booking as _add_booking
            _add_booking(
                target=d,
                guests=guests,
                name=name,
                phone=phone,
                source=source,
                client_type="",
                comment=f"[Голос] {transcript}" if transcript else "",
                changed_by=uname,
            )
            database.upsert_booking(
                target=d, name=name, guests=guests, phone=phone,
                source=source, weekday=weekday, changed_by=uname,
                comment=f"[Голос] {transcript}" if transcript else "",
            )
            lines = [f"📅 {_e(dt_str)}"]
            if name:   lines.append(f"👤 {_e(name)}")
            if guests: lines.append(f"👥 {guests} гостей")
            if phone:  lines.append(f"📞 {_e(phone)}")
            if source: lines.append(f"📢 {_e(source)}")
            await q.edit_message_text(
                "✅ Бронь добавлена\n" + "\n".join(lines), parse_mode="HTML",
            )
            log.info("🎙 +бронь голосом: %s %s г.%s (@%s)", dt_str, name, guests, uname)

        elif action == "delete":
            from sheets import remove_booking as _remove_booking
            _remove_booking(target=d)
            database.delete_booking(target=d)
            await q.edit_message_text(
                f"✅ Бронь удалена\n📅 {_e(dt_str)}", parse_mode="HTML",
            )
            log.info("🎙 -бронь голосом: %s (@%s)", dt_str, uname)

        elif action == "edit":
            fields = pending.get("fields") or {}
            if not fields:
                await q.edit_message_text("❌ Нет полей для обновления.")
                return

            # Перенос даты брони (new_date — особый случай)
            new_date_str = fields.pop("new_date", None)
            if new_date_str:
                try:
                    new_d = datetime.strptime(new_date_str, "%d.%m.%Y").date()
                except ValueError:
                    await q.edit_message_text(f"❌ Неверная новая дата: {_e(new_date_str)}", parse_mode="HTML")
                    return
                # Переносим: копируем бронь на новую дату, удаляем старую
                booking = _booking_by_date(dt_str)
                if booking:
                    weekday_new = _WD[new_d.weekday()]
                    merged = {**booking, **fields, "date": new_date_str, "weekday": weekday_new}
                    from sheets import add_booking as _add_b, remove_booking as _del_b
                    try:
                        _add_b(target=new_d, guests=merged.get("guests",""), name=merged.get("name",""),
                               phone=merged.get("phone",""), source=merged.get("source",""),
                               client_type=merged.get("client_type",""), comment=merged.get("comment",""),
                               changed_by=uname)
                        _del_b(target=d)
                    except Exception as _se:
                        log.warning("Sheets date-move: %s", _se)
                    database.upsert_booking(target=new_d, name=merged.get("name",""),
                                            weekday=weekday_new, changed_by=uname)
                    database.delete_booking(target=d)
                await q.edit_message_text(
                    f"✅ <b>Дата брони перенесена</b>\n{_e(dt_str)} → <b>{_e(new_date_str)}</b>",
                    parse_mode="HTML")
                log.info("🎙 перенос даты голосом: %s→%s (@%s)", dt_str, new_date_str, uname)
                return

            # Обычное редактирование полей
            database.update_booking_fields(d, changed_by=uname, **fields)
            from sheets import edit_booking as _edit_booking
            try:
                _edit_booking(target=d, changed_by=uname, **fields)
            except Exception as _se:
                log.warning("Sheets sync after voice edit: %s", _se)
            result_lines = [f"✅ <b>Обновлено</b>\n📅 {_e(dt_str)}"]
            for k, v in fields.items():
                result_lines.append(f"  {_FIELD_LABELS.get(k,k)}: {_fmt_field_val(k,v)}")
            await q.edit_message_text("\n".join(result_lines), parse_mode="HTML")
            log.info("🎙 edit голосом: %s %s (@%s)", dt_str, fields, uname)

    except Exception as exc:
        log.exception("Ошибка выполнения голосовой команды")
        await q.edit_message_text(f"❌ Ошибка: {_e(str(exc))}", parse_mode="HTML")


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
# Алиасы из utils для обратной совместимости внутри модуля
_MO = MONTHS
_WD = WEEKDAYS_SHORT
_MONTHS_RU = MONTHS_RU

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

def fmt_date(d: date) -> str:
    return f"{d.strftime('%d.%m.%Y')} ({_WD[d.weekday()]})"


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


_PRICE_KEYWORDS    = {"цена","цены","стоимость","сколько стоит","прайс",
                      "расценки","сколько","почём","почем","аренда стоит"}
_CAPACITY_KEYWORDS = {"вместимость","вместить","вмещает","сколько человек",
                      "человек максимум","вместит"}


_has_phone   = has_phone
_has_tg_nick = has_tg_nick


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

    # Персональное приветствие для администраторов
    _GREETINGS = {"привет", "хай", "хеллоу", "hello", "hi", "здравствуй",
                  "здравствуйте", "добрый день", "добрый вечер", "доброе утро", "ку"}
    if tl in _GREETINGS and is_admin(update):
        first = update.effective_user.first_name or "друг"
        await update.message.reply_text(f"Привет, {first}! 👋")
        return

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

    try:
        _is_new = not database.has_seen_user(user.id)
    except Exception as _db_err:
        log.warning("   ↳ has_seen_user ошибка: %s — считаем пользователя знакомым", _db_err)
        _is_new = False
    if _is_new and not is_admin(update):
        try:
            database.mark_user_seen(user.id)
        except Exception as _db_err:
            log.warning("   ↳ mark_user_seen ошибка: %s", _db_err)
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
        text = _HELP_TEXT
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
    log.info(f"  SPREADSHEET_ID : {SPREADSHEET_ID or '⚠️  НЕ ЗАДАН'}")
    log.info(f"  credentials    : {'✅' if os.path.exists(GOOGLE_CREDENTIALS_PATH) else '❌ НЕ НАЙДЕН'}")

    # Авито поллер — запускается если заданы ключи в .env
    _avito_client = None
    if AVITO_CLIENT_ID and AVITO_CLIENT_SECRET:
        try:
            from avito      import AvitoClient
            from avito_poll import avito_polling_loop
            _avito_client = AvitoClient(
                client_id     = AVITO_CLIENT_ID,
                client_secret = AVITO_CLIENT_SECRET,
                name          = AVITO_ACCOUNT_NAME,
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
    app.add_handler(CallbackQueryHandler(avito_callback,   pattern=r"^av_"))
    app.add_handler(CallbackQueryHandler(voice_confirm_cb, pattern=r"^voice:"))

    # Голосовые команды (только для администраторов)
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

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
