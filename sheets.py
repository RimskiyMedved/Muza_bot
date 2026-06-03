"""
sheets.py — работа с Google Таблицей.

Два рабочих листа:
  «Бронирования» — только занятые даты с данными клиента
  «Свободные»    — список свободных дат (ведётся менеджером вручную)
  «Авито»        — лиды из Авито (имя + контакт)

Структура «Бронирования»:
  A     B              C            D        E                 F                          G            H
  Дата  Кол-во гостей  Имя клиента  Телефон  Источник рекламы  Прямой клиент/Агентство  Комментарий  День недели

Структура «Свободные»:
  A     B
  Дата  День недели

Структура «Авито»:
  A           B    C        D       E
  Дата/Время  Имя  Телефон  Ник ТГ  Объявление

Логика синхронизации:
  add_booking()    → добавляет в «Бронирования» + удаляет из «Свободные»
  remove_booking() → удаляет из «Бронирования» + возвращает в «Свободные»
  edit_booking()   → обновляет поля в «Бронирования»
"""

import logging
import os
import re
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

from config import (
    SPREADSHEET_ID,
    GOOGLE_CREDENTIALS_PATH,
    SHEET_NAME,
    FREE_SHEET_NAME,
    LEADS_SHEET_NAME,
)
from utils import normalize_phone

# SQLite-зеркало (необязательная зависимость)
try:
    import database as _db
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False

_sh_log = logging.getLogger("SHEETS")

SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
DATE_FMT = "%d.%m.%Y"

WEEKDAYS = [
    "Понедельник", "Вторник", "Среда",
    "Четверг", "Пятница", "Суббота", "Воскресенье",
]

HEADERS_BOOKINGS = [
    "Дата", "Кол-во гостей", "Имя клиента", "Телефон",
    "Источник рекламы", "Прямой клиент / Агентство", "Комментарий", "День недели",
    "Изменил", "Дата изм.", "Ник ТГ",
]
HEADERS_FREE  = ["Дата", "День недели"]
HEADERS_LEADS = ["Дата/Время", "Имя", "Телефон", "Ник ТГ", "Объявление"]


# ─── Кеш (единый для всех листов) ────────────────────────────────────────────

_caches: dict[str, tuple[list[list], datetime]] = {}
_CACHE_TTL = timedelta(minutes=5)


def _get_cached(ws: gspread.Worksheet) -> list[list]:
    """Возвращает данные листа из кеша или читает из Google."""
    key = ws.title
    if key in _caches:
        data, ts = _caches[key]
        if (datetime.now() - ts) < _CACHE_TTL:
            return data
    data = ws.get_all_values()
    _caches[key] = (data, datetime.now())
    return data


def _invalidate(sheet_name: str) -> None:
    _caches.pop(sheet_name, None)


# ─── Подключение (с кешем) ────────────────────────────────────────────────────

_gc_client: gspread.Client | None = None
_spreadsheet_cache: gspread.Spreadsheet | None = None


def _spreadsheet() -> gspread.Spreadsheet:
    global _gc_client, _spreadsheet_cache
    if _gc_client is None:
        creds = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_PATH,
            scopes=SCOPES,
        )
        _gc_client = gspread.authorize(creds)
        _spreadsheet_cache = None
    if _spreadsheet_cache is None:
        _spreadsheet_cache = _gc_client.open_by_key(SPREADSHEET_ID)
    return _spreadsheet_cache


def _sheet_bookings() -> gspread.Worksheet:
    return _spreadsheet().worksheet(SHEET_NAME)


def _sheet_free() -> gspread.Worksheet:
    return _spreadsheet().worksheet(FREE_SHEET_NAME)


def _sheet_leads() -> gspread.Worksheet:
    return _spreadsheet().worksheet(LEADS_SHEET_NAME)


# ─── Внутренние утилиты ───────────────────────────────────────────────────────

def _sort_key(row: list) -> date:
    try:
        return datetime.strptime(row[0].strip(), DATE_FMT).date()
    except (ValueError, IndexError):
        return date(9999, 12, 31)


def _data_rows(ws: gspread.Worksheet) -> list[list]:
    """Строки данных без заголовка, с кешем."""
    rows = _get_cached(ws)
    if not rows:
        return []
    start = 1 if (rows[0] and rows[0][0] in ("Дата", "Дата/Время")) else 0
    return [list(r) for r in rows[start:] if r and any(c.strip() for c in r)]


def _rewrite_sheet(ws: gspread.Worksheet, headers: list, data_rows: list[list]) -> None:
    """
    Перезаписывает лист одним вызовом API (без ws.clear() → ws.update()).

    Вместо двух вызовов (clear + write) делаем один: записываем новые данные
    и затираем лишние строки пустыми значениями. Это исключает потерю данных
    при сбое между двумя API-запросами.
    """
    data_rows.sort(key=_sort_key)
    new_data = [headers] + data_rows

    # Определяем сколько строк сейчас в листе (через кеш, без лишнего API-запроса)
    cached = _caches.get(ws.title)
    old_row_count = len(cached[0]) if cached else len(new_data)

    # Если старых строк больше — дописываем пустые строки для перезаписи остатка
    if old_row_count > len(new_data):
        blank = [""] * len(headers)
        upload = new_data + [blank] * (old_row_count - len(new_data))
    else:
        upload = new_data

    ws.update("A1", upload, value_input_option="USER_ENTERED")
    _invalidate(ws.title)


def _weekday(d: date) -> str:
    return WEEKDAYS[d.weekday()]


# ─── Лист «Свободные» ────────────────────────────────────────────────────────

def get_free_dates(limit: int = 10) -> list[str]:
    """Возвращает ближайшие свободные даты из листа «Свободные»."""
    ws = _sheet_free()
    today = date.today()
    result = []
    for row in _data_rows(ws):
        try:
            d = datetime.strptime(row[0].strip(), DATE_FMT).date()
            if d >= today:
                result.append((d, row[0].strip()))
        except ValueError:
            pass
    result.sort(key=lambda x: x[0])
    return [s for _, s in result][:limit]


def _remove_from_free(target: date) -> None:
    """Удаляет дату из «Свободные» если она там есть."""
    ws = _sheet_free()
    target_str = target.strftime(DATE_FMT)
    rows = _data_rows(ws)
    new_rows = [r for r in rows if r[0].strip() != target_str]
    if len(new_rows) < len(rows):
        _rewrite_sheet(ws, HEADERS_FREE, new_rows)


def _add_to_free(target: date) -> None:
    """Добавляет дату в «Свободные» (только если >= сегодня и её ещё нет)."""
    if target < date.today():
        return
    ws = _sheet_free()
    target_str = target.strftime(DATE_FMT)
    rows = _data_rows(ws)
    if any(r[0].strip() == target_str for r in rows):
        return
    rows.append([target_str, _weekday(target)])
    _rewrite_sheet(ws, HEADERS_FREE, rows)


# ─── Лист «Бронирования» ─────────────────────────────────────────────────────

def check_date(target: date) -> dict:
    """
    Ищет дату в «Бронирования».
    found=True → занята, found=False → свободна.
    """
    ws = _sheet_bookings()
    target_str = target.strftime(DATE_FMT)
    for i, row in enumerate(_get_cached(ws), start=1):
        if not row or row[0].strip() != target_str:
            continue
        def v(idx): return row[idx].strip() if len(row) > idx else ""
        return {
            "found":       True,
            "guests":      v(1),
            "name":        v(2),
            "phone":       v(3),
            "source":      v(4),
            "client_type": v(5),
            "comment":     v(6),
            "weekday":     v(7),
            "tg_nick":     v(10),
            "row":         i,
        }
    return {"found": False, "row": None}


def get_all_bookings() -> list[dict]:
    """Все записи из «Бронирования», отсортированные по дате."""
    ws = _sheet_bookings()
    today = date.today()
    result = []
    for row in _data_rows(ws):
        try:
            d = datetime.strptime(row[0].strip(), DATE_FMT).date()
            result.append({
                "date":        row[0].strip(),
                "date_obj":    d,
                "guests":      row[1].strip() if len(row) > 1 else "",
                "name":        row[2].strip() if len(row) > 2 else "",
                "phone":       row[3].strip() if len(row) > 3 else "",
                "source":      row[4].strip() if len(row) > 4 else "",
                "client_type": row[5].strip() if len(row) > 5 else "",
                "comment":     row[6].strip() if len(row) > 6 else "",
                "weekday":     row[7].strip() if len(row) > 7 else "",
                "tg_nick":     row[10].strip() if len(row) > 10 else "",
                "future":      d >= today,
            })
        except ValueError:
            pass
    result.sort(key=lambda r: r["date_obj"])
    return result


def add_booking(
    target: date,
    guests: str,
    name: str,
    phone: str,
    source: str,
    client_type: str,
    comment: str,
    changed_by: str = "",
    tg_nick: str = "",
) -> None:
    """
    Добавляет или перезаписывает бронь.
    Автоматически удаляет дату из листа «Свободные».
    """
    ws = _sheet_bookings()
    target_str = target.strftime(DATE_FMT)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    new_row = [
        target_str, str(guests), name, phone,
        source, client_type, comment, _weekday(target),
        changed_by, now_str, tg_nick,
    ]
    rows = _data_rows(ws)
    found = False
    for i, row in enumerate(rows):
        if row and row[0].strip() == target_str:
            rows[i] = new_row
            found = True
            break
    if not found:
        rows.append(new_row)
    _rewrite_sheet(ws, HEADERS_BOOKINGS, rows)
    _remove_from_free(target)

    # ── Зеркало → SQLite ──────────────────────────────────────────────────────
    if _DB_AVAILABLE:
        try:
            _db.upsert_booking(
                target, guests=guests, name=name, phone=phone,
                source=source, client_type=client_type, comment=comment,
                weekday=_weekday(target), changed_by=changed_by, tg_nick=tg_nick,
            )
            _db.remove_free_date(target)
        except Exception as _e:
            _sh_log.warning("SQLite upsert_booking error: %s", _e)


def remove_booking(target: date) -> bool:
    """
    Удаляет бронь из «Бронирования».
    Автоматически возвращает дату в «Свободные» (если >= сегодня).
    Возвращает True если запись найдена и удалена.
    """
    ws = _sheet_bookings()
    target_str = target.strftime(DATE_FMT)
    rows = _data_rows(ws)
    new_rows = [r for r in rows if r[0].strip() != target_str]
    if len(new_rows) == len(rows):
        return False
    _rewrite_sheet(ws, HEADERS_BOOKINGS, new_rows)
    _add_to_free(target)

    # ── Зеркало → SQLite ──────────────────────────────────────────────────────
    if _DB_AVAILABLE:
        try:
            _db.delete_booking(target)
            if target >= date.today():
                _db.add_free_date(target, _weekday(target))
        except Exception as _e:
            _sh_log.warning("SQLite delete_booking error: %s", _e)

    return True


def edit_booking(target: date, changed_by: str = "", **fields) -> bool:
    """
    Редактирует поля существующей брони.
    Допустимые ключи: guests, name, phone, source, client_type, comment
    Возвращает True если строка найдена и обновлена.
    """
    ws = _sheet_bookings()
    target_str = target.strftime(DATE_FMT)
    field_map = {
        "guests": 1, "name": 2, "phone": 3,
        "source": 4, "client_type": 5, "comment": 6, "tg_nick": 10,
    }
    rows = _data_rows(ws)
    for i, row in enumerate(rows):
        if not row or row[0].strip() != target_str:
            continue
        while len(row) < 11:
            row.append("")
        for field, value in fields.items():
            if field in field_map:
                row[field_map[field]] = str(value)
        row[8] = changed_by
        row[9] = datetime.now().strftime("%d.%m.%Y %H:%M")
        rows[i] = row
        _rewrite_sheet(ws, HEADERS_BOOKINGS, rows)

        # ── Зеркало → SQLite ──────────────────────────────────────────────────
        if _DB_AVAILABLE:
            try:
                _db.update_booking_fields(target, changed_by=changed_by, **fields)
            except Exception as _e:
                _sh_log.warning("SQLite update_booking_fields error: %s", _e)

        return True
    return False


# ─── Лист «Авито» (лиды) ─────────────────────────────────────────────────────

def add_lead(name: str, phone: str = "", nick: str = "", source: str = "Авито") -> None:
    """
    Записывает лид в лист «Авито».
    Дедупликация по телефону:
      - Если телефон уже есть → обновляем ник (если пришёл новый) и не дублируем строку.
      - Если только ник (без телефона) → всегда добавляем новую строку.
    """
    ws = _sheet_leads()
    rows = _get_cached(ws)

    # Создаём заголовок если листа нет
    if not rows or not rows[0] or rows[0][0] != "Дата/Время":
        ws.clear()
        ws.update("A1", [HEADERS_LEADS], value_input_option="USER_ENTERED")
        _invalidate(ws.title)
        rows = [HEADERS_LEADS]

    # Проверяем дубль по телефону
    if phone:
        norm_new = normalize_phone(phone)
        data_rows = rows[1:] if rows[0][0] == "Дата/Время" else rows
        for i, row in enumerate(data_rows, start=2):  # +2: заголовок + 1-based
            existing_phone = row[2].strip() if len(row) > 2 else ""
            if existing_phone and normalize_phone(existing_phone) == norm_new:
                # Телефон уже есть — добавляем ник если его не было
                if nick:
                    existing_nick = row[3].strip() if len(row) > 3 else ""
                    if not existing_nick:
                        ws.update_cell(i, 4, nick)
                        _invalidate(ws.title)
                return  # дубль, не добавляем новую строку

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    ws.append_row([now, name, phone, nick, source], value_input_option="USER_ENTERED")
    _invalidate(ws.title)

    # ── Зеркало → SQLite ──────────────────────────────────────────────────────
    if _DB_AVAILABLE:
        try:
            _db.add_lead(name=name, phone=phone, nick=nick, source=source)
        except Exception as _e:
            _sh_log.warning("SQLite add_lead error: %s", _e)
