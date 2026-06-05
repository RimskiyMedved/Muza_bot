"""
database.py — SQLite-зеркало Google Таблицы.

Логика:
  - Записи идут в Google Sheets (основной источник) + сюда параллельно
  - Чтение (check_date, get_all_bookings) — из SQLite (быстро, без API)
  - При старте бота: sync_from_sheets() подтягивает актуальные данные из Sheets
  - Если Sheets недоступен — бот работает из SQLite

Файл базы: muza.db (рядом с bot.py)
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from utils import normalize_phone

log = logging.getLogger("DB")

DB_PATH  = os.path.join(os.path.dirname(__file__), "database.db")
DATE_FMT = "%d.%m.%Y"

# ─── Настройки (редактируемые через админку) ─────────────────────────────────

SETTINGS_DEFAULTS: dict[str, float] = {
    "cost_manager": 15_000.0,
    "cost_chef":    18_000.0,
    "cost_waiter":  7_000.0,
    "cost_cook":    7_000.0,
    "agency_pct":   10.0,     # % от меню (для агентства)
}

SETTINGS_LABELS: dict[str, str] = {
    "cost_manager": "Менеджер (за банкет), ₽",
    "cost_chef":    "Шеф-повар (за банкет), ₽",
    "cost_waiter":  "Официант (за банкет), ₽",
    "cost_cook":    "Повар (за банкет), ₽",
    "agency_pct":   "Агентский процент, %",
}

_rates_cache: dict | None = None
_rates_cache_ts: float = 0.0
_RATES_CACHE_TTL = 60.0   # секунд


def _get_rates() -> dict:
    """Возвращает настройки ставок из кеша (обновляется раз в 60 с)."""
    import time
    global _rates_cache, _rates_cache_ts
    if _rates_cache is None or (time.time() - _rates_cache_ts) > _RATES_CACHE_TTL:
        try:
            _rates_cache = get_settings()
        except Exception:
            _rates_cache = dict(SETTINGS_DEFAULTS)
        _rates_cache_ts = time.time()
    return _rates_cache


def _invalidate_rates_cache() -> None:
    global _rates_cache
    _rates_cache = None


def compute_financials(booking: dict, rates: dict | None = None) -> dict:
    """Вычисляет финансовые показатели банкета. rates=None → читает из БД (кеш 60 с)."""
    if rates is None:
        rates = _get_rates()
    cost_manager = float(rates.get("cost_manager", SETTINGS_DEFAULTS["cost_manager"]))
    cost_chef    = float(rates.get("cost_chef",    SETTINGS_DEFAULTS["cost_chef"]))
    cost_waiter  = float(rates.get("cost_waiter",  SETTINGS_DEFAULTS["cost_waiter"]))
    cost_cook    = float(rates.get("cost_cook",    SETTINGS_DEFAULTS["cost_cook"]))
    agency_pct   = float(rates.get("agency_pct",   SETTINGS_DEFAULTS["agency_pct"])) / 100.0

    revenue_rent  = float(booking.get("revenue_rent")  or 0)
    revenue_menu  = float(booking.get("revenue_menu")  or 0)
    staff_waiters = int(booking.get("staff_waiters")   or 0)
    staff_cooks   = int(booking.get("staff_cooks")     or 0)
    is_agency     = (booking.get("client_type") or "").strip() == "Агентство"

    cost_waiters   = staff_waiters * cost_waiter
    cost_cooks     = staff_cooks   * cost_cook
    agency_fee     = round(revenue_menu * agency_pct, 2) if is_agency else 0.0
    total_income   = revenue_rent + revenue_menu
    total_expenses = cost_manager + cost_chef + cost_waiters + cost_cooks + agency_fee
    return {
        "total_income":   total_income,
        "cost_manager":   cost_manager,
        "cost_chef":      cost_chef,
        "cost_waiters":   cost_waiters,
        "cost_cooks":     cost_cooks,
        "agency_fee":     agency_fee,
        "agency_pct":     agency_pct * 100,
        "total_expenses": total_expenses,
        "profit":         total_income - total_expenses,
    }


def _to_iso(date_str: str) -> str:
    """'15.06.2026' → '2026-06-15' для SQL ORDER BY / WHERE."""
    if len(date_str) == 10 and date_str[2] == "." and date_str[5] == ".":
        return f"{date_str[6:10]}-{date_str[3:5]}-{date_str[0:2]}"
    return ""


# ─── Подключение ──────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ─── Инициализация схемы ──────────────────────────────────────────────────────

def init_db() -> None:
    """Создаёт таблицы если их нет. Вызывается при старте бота."""
    # WAL-режим: лучше переносит одновременные записи из бота и API
    with _conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                date          TEXT PRIMARY KEY,
                date_iso      TEXT DEFAULT '',
                guests        TEXT DEFAULT '',
                name          TEXT DEFAULT '',
                phone         TEXT DEFAULT '',
                source        TEXT DEFAULT '',
                client_type   TEXT DEFAULT '',
                comment       TEXT DEFAULT '',
                weekday       TEXT DEFAULT '',
                changed_by    TEXT DEFAULT '',
                changed_at    TEXT DEFAULT '',
                synced_at     TEXT DEFAULT '',
                contract_date TEXT DEFAULT '',
                revenue_rent  REAL DEFAULT 0,
                revenue_menu  REAL DEFAULT 0,
                paid_advance  REAL DEFAULT 0,
                paid_rent     REAL DEFAULT 0,
                paid_final    REAL DEFAULT 0,
                staff_waiters      INTEGER DEFAULT 0,
                staff_cooks        INTEGER DEFAULT 0,
                paid_advance_date  TEXT DEFAULT '',
                paid_rent_date     TEXT DEFAULT '',
                paid_final_date    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS free_dates (
                date         TEXT PRIMARY KEY,   -- дд.мм.гггг
                date_iso     TEXT DEFAULT '',    -- YYYY-MM-DD для SQL-сортировки
                weekday      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS leads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                datetime     TEXT,
                name         TEXT DEFAULT '',
                phone        TEXT DEFAULT '',
                phone_norm   TEXT DEFAULT '',   -- цифры 79XXXXXXXXX для быстрого поиска
                nick         TEXT DEFAULT '',
                source       TEXT DEFAULT '',
                synced_at    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS allowed_users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER UNIQUE,
                username     TEXT DEFAULT '',
                added_at     TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS access_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER,
                username     TEXT DEFAULT '',
                action       TEXT DEFAULT '',
                timestamp    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS seen_users (
                telegram_id  INTEGER PRIMARY KEY,
                seen_at      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS avito_chat_state (
                chat_id          TEXT PRIMARY KEY,
                greeted          INTEGER DEFAULT 0,
                awaiting_contact INTEGER DEFAULT 0,
                asked_date       INTEGER DEFAULT 0,
                lead_received    INTEGER DEFAULT 0,
                offered_alt      INTEGER DEFAULT 0,
                faq_count        INTEGER DEFAULT 0,
                stuck_notified   INTEGER DEFAULT 0,
                ignored          INTEGER DEFAULT 0,
                last_handled_id  TEXT DEFAULT '',
                ctx_date         TEXT DEFAULT '',
                ctx_guests       TEXT DEFAULT '',
                ctx_event        TEXT DEFAULT '',
                ctx_phone        TEXT DEFAULT '',
                updated_at       TEXT DEFAULT ''
            );
        """)

    # ── Миграции: добавляем колонки в уже существующие БД ────────────────────
    _safe_alter("ALTER TABLE bookings ADD COLUMN date_iso TEXT DEFAULT ''")
    _safe_alter("ALTER TABLE free_dates ADD COLUMN date_iso TEXT DEFAULT ''")
    _safe_alter("ALTER TABLE leads ADD COLUMN phone_norm TEXT DEFAULT ''")
    # Финансовые поля (v2)
    _safe_alter("ALTER TABLE bookings ADD COLUMN contract_date TEXT DEFAULT ''")
    _safe_alter("ALTER TABLE bookings ADD COLUMN revenue_rent REAL DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN revenue_menu REAL DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_advance REAL DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_rent REAL DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_final REAL DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN staff_waiters INTEGER DEFAULT 0")
    _safe_alter("ALTER TABLE bookings ADD COLUMN staff_cooks INTEGER DEFAULT 0")
    # Даты оплат (v3)
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_advance_date TEXT DEFAULT ''")
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_rent_date TEXT DEFAULT ''")
    _safe_alter("ALTER TABLE bookings ADD COLUMN paid_final_date TEXT DEFAULT ''")

    # ── Заполняем настройки по умолчанию ─────────────────────────────────────
    with _conn() as con:
        for key, value in SETTINGS_DEFAULTS.items():
            con.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                (key, str(value)),
            )

    # ── Заполняем date_iso для существующих строк (одноразовая миграция) ─────
    with _conn() as con:
        con.execute("""
            UPDATE bookings SET
                date_iso = substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2)
            WHERE date_iso = '' OR date_iso IS NULL
        """)
        con.execute("""
            UPDATE free_dates SET
                date_iso = substr(date,7,4)||'-'||substr(date,4,2)||'-'||substr(date,1,2)
            WHERE date_iso = '' OR date_iso IS NULL
        """)

    # ── Заполняем phone_norm для существующих лидов ───────────────────────────
    with _conn() as con:
        rows = con.execute(
            "SELECT id, phone FROM leads WHERE (phone_norm = '' OR phone_norm IS NULL) AND phone != ''"
        ).fetchall()
    if rows:
        with _conn() as con:
            for row in rows:
                con.execute(
                    "UPDATE leads SET phone_norm = ? WHERE id = ?",
                    (normalize_phone(row["phone"]), row["id"]),
                )

    log.info("✅ SQLite инициализирована: %s", DB_PATH)


def _safe_alter(sql: str) -> None:
    """Выполняет ALTER TABLE, молча игнорирует если колонка уже есть."""
    try:
        with _conn() as con:
            con.execute(sql)
    except Exception:
        pass


# ─── Настройки ────────────────────────────────────────────────────────────────

def get_settings() -> dict:
    """Возвращает все настройки из БД, дополняя значениями по умолчанию."""
    with _conn() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    result = dict(SETTINGS_DEFAULTS)
    for row in rows:
        if row["key"] in result:
            try:
                result[row["key"]] = float(row["value"])
            except (ValueError, TypeError):
                pass
    return result


def update_setting(key: str, value: float) -> bool:
    """Обновляет одну настройку. Возвращает False если ключ неизвестен."""
    if key not in SETTINGS_DEFAULTS:
        return False
    with _conn() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    _invalidate_rates_cache()
    log.info("⚙️  Настройка обновлена: %s = %s", key, value)
    return True


# ─── Синхронизация из Google Sheets ──────────────────────────────────────────

def sync_from_sheets() -> None:
    """
    Подтягивает актуальные данные из Google Sheets в SQLite.
    Вызывается при старте бота. Не блокирует запуск при ошибке.
    """
    try:
        from sheets import get_all_bookings, get_free_dates, _sheet_leads, _data_rows
        now = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Бронирования
        bookings = get_all_bookings()
        if not bookings:
            log.warning("⚠️  Sheets вернул 0 бронирований — пропускаем sync во избежание потери данных")
            return
        with _conn() as con:
            con.execute("DELETE FROM bookings")
            for b in bookings:
                con.execute("""
                    INSERT INTO bookings
                    (date, date_iso, guests, name, phone, source, client_type, comment,
                     weekday, changed_by, changed_at, synced_at,
                     contract_date,
                     revenue_rent, revenue_menu, paid_advance, paid_rent, paid_final,
                     staff_waiters, staff_cooks,
                     paid_advance_date, paid_rent_date, paid_final_date)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (b["date"], _to_iso(b["date"]), b["guests"], b["name"], b["phone"],
                      b["source"], b["client_type"], b["comment"],
                      b.get("weekday", ""), "", "", now,
                      b.get("contract_date", ""),
                      b.get("revenue_rent", 0), b.get("revenue_menu", 0),
                      b.get("paid_advance", 0), b.get("paid_rent", 0), b.get("paid_final", 0),
                      b.get("staff_waiters", 0), b.get("staff_cooks", 0),
                      b.get("paid_advance_date", ""), b.get("paid_rent_date", ""),
                      b.get("paid_final_date", "")))
        log.info("✅ Синхронизировано бронирований: %d", len(bookings))

        # Свободные даты
        free = get_free_dates(limit=9999)
        with _conn() as con:
            con.execute("DELETE FROM free_dates")
            for d_str in free:
                try:
                    d = datetime.strptime(d_str, DATE_FMT).date()
                    from sheets import _weekday
                    con.execute("INSERT INTO free_dates (date, date_iso, weekday) VALUES (?,?,?)",
                                (d_str, d.strftime("%Y-%m-%d"), _weekday(d)))
                except ValueError:
                    pass
        log.info("✅ Синхронизировано свободных дат: %d", len(free))

        # Лиды
        ws = _sheet_leads()
        rows = _data_rows(ws)
        with _conn() as con:
            con.execute("DELETE FROM leads")
            for row in rows:
                _phone = row[2] if len(row) > 2 else ""
                con.execute("""
                    INSERT INTO leads (datetime, name, phone, phone_norm, nick, source, synced_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    row[0] if len(row) > 0 else "",
                    row[1] if len(row) > 1 else "",
                    _phone,
                    normalize_phone(_phone),
                    row[3] if len(row) > 3 else "",
                    row[4] if len(row) > 4 else "",
                    now,
                ))
        log.info("✅ Синхронизировано лидов: %d", len(rows))

    except Exception as e:
        log.warning("⚠️  Синхронизация из Sheets не удалась: %s — работаем из кеша БД", e)


# ─── Бронирования ─────────────────────────────────────────────────────────────

def check_date(target: date) -> dict:
    """Быстрая проверка даты из SQLite."""
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM bookings WHERE date = ?", (target_str,)
        ).fetchone()
    if row:
        keys = row.keys()
        def _f(k): return float(row[k] or 0) if k in keys else 0.0
        def _i(k): return int(row[k] or 0) if k in keys else 0
        def _s(k, d=""): return row[k] if k in keys else d
        b = {
            "found": True,
            "guests": row["guests"], "name": row["name"], "phone": row["phone"],
            "source": row["source"], "client_type": row["client_type"],
            "comment": row["comment"], "weekday": row["weekday"],
            "contract_date": _s("contract_date"),
            "revenue_rent": _f("revenue_rent"), "revenue_menu": _f("revenue_menu"),
            "paid_advance": _f("paid_advance"), "paid_rent": _f("paid_rent"),
            "paid_final": _f("paid_final"),
            "staff_waiters": _i("staff_waiters"), "staff_cooks": _i("staff_cooks"),
            "paid_advance_date": _s("paid_advance_date"),
            "paid_rent_date":    _s("paid_rent_date"),
            "paid_final_date":   _s("paid_final_date"),
        }
        b.update(compute_financials(b))
        return b
    return {"found": False}


def get_all_bookings() -> list[dict]:
    """Все бронирования из SQLite, отсортированные по дате (SQL ORDER BY date_iso)."""
    today = date.today()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM bookings ORDER BY date_iso, date"
        ).fetchall()
    result = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], DATE_FMT).date()
            keys = row.keys()
            def _f(k): return float(row[k] or 0) if k in keys else 0.0
            def _i(k): return int(row[k] or 0) if k in keys else 0
            def _s(k, dv=""): return row[k] if k in keys else dv
            b = {
                "date": row["date"], "date_obj": d, "future": d >= today,
                "guests": row["guests"], "name": row["name"], "phone": row["phone"],
                "source": row["source"], "client_type": row["client_type"],
                "comment": row["comment"], "weekday": row["weekday"],
                "contract_date": _s("contract_date"),
                "revenue_rent": _f("revenue_rent"), "revenue_menu": _f("revenue_menu"),
                "paid_advance": _f("paid_advance"), "paid_rent": _f("paid_rent"),
                "paid_final": _f("paid_final"),
                "staff_waiters": _i("staff_waiters"), "staff_cooks": _i("staff_cooks"),
                "paid_advance_date": _s("paid_advance_date"),
                "paid_rent_date":    _s("paid_rent_date"),
                "paid_final_date":   _s("paid_final_date"),
            }
            b.update(compute_financials(b))
            result.append(b)
        except ValueError:
            pass
    return result


def upsert_booking(
    target: date,
    guests: str = "",
    name: str = "",
    phone: str = "",
    source: str = "",
    client_type: str = "",
    comment: str = "",
    weekday: str = "",
    changed_by: str = "",
    contract_date: str = "",
    revenue_rent: float = 0,
    revenue_menu: float = 0,
    paid_advance: float = 0,
    paid_rent: float = 0,
    paid_final: float = 0,
    staff_waiters: int = 0,
    staff_cooks: int = 0,
    paid_advance_date: str = "",
    paid_rent_date: str = "",
    paid_final_date: str = "",
) -> None:
    """Добавляет или обновляет бронь в SQLite."""
    target_str = target.strftime(DATE_FMT)
    date_iso   = target.strftime("%Y-%m-%d")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute("""
            INSERT INTO bookings
            (date, date_iso, guests, name, phone, source, client_type, comment,
             weekday, changed_by, changed_at, synced_at,
             contract_date, revenue_rent, revenue_menu,
             paid_advance, paid_rent, paid_final, staff_waiters, staff_cooks,
             paid_advance_date, paid_rent_date, paid_final_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                date_iso=excluded.date_iso,
                guests=excluded.guests, name=excluded.name, phone=excluded.phone,
                source=excluded.source, client_type=excluded.client_type,
                comment=excluded.comment, weekday=excluded.weekday,
                changed_by=excluded.changed_by, changed_at=excluded.changed_at,
                synced_at=excluded.synced_at,
                contract_date=excluded.contract_date,
                revenue_rent=excluded.revenue_rent, revenue_menu=excluded.revenue_menu,
                paid_advance=excluded.paid_advance, paid_rent=excluded.paid_rent,
                paid_final=excluded.paid_final,
                staff_waiters=excluded.staff_waiters, staff_cooks=excluded.staff_cooks,
                paid_advance_date=excluded.paid_advance_date,
                paid_rent_date=excluded.paid_rent_date,
                paid_final_date=excluded.paid_final_date
        """, (target_str, date_iso, guests, name, phone, source, client_type,
              comment, weekday, changed_by, now, now,
              contract_date, revenue_rent, revenue_menu,
              paid_advance, paid_rent, paid_final, staff_waiters, staff_cooks,
              paid_advance_date, paid_rent_date, paid_final_date))


def delete_booking(target: date) -> bool:
    """Удаляет бронь из SQLite. Возвращает True если запись была."""
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        cur = con.execute("DELETE FROM bookings WHERE date = ?", (target_str,))
    return cur.rowcount > 0


def update_booking_fields(target: date, changed_by: str = "", **fields) -> bool:
    """Обновляет отдельные поля брони."""
    target_str = target.strftime(DATE_FMT)
    allowed = {
        "guests", "name", "phone", "source", "client_type", "comment",
        "contract_date",
        "revenue_rent", "revenue_menu", "paid_advance", "paid_rent", "paid_final",
        "staff_waiters", "staff_cooks",
        "paid_advance_date", "paid_rent_date", "paid_final_date",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    updates["changed_by"] = changed_by
    updates["changed_at"] = now
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [target_str]
    with _conn() as con:
        cur = con.execute(
            f"UPDATE bookings SET {set_clause} WHERE date = ?", values
        )
    return cur.rowcount > 0


# ─── Свободные даты ───────────────────────────────────────────────────────────

def get_free_dates(limit: int = 10) -> list[str]:
    """Ближайшие свободные даты из SQLite (SQL ORDER BY date_iso, фильтр >= сегодня)."""
    today_iso = date.today().strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            "SELECT date FROM free_dates WHERE date_iso >= ? ORDER BY date_iso LIMIT ?",
            (today_iso, limit),
        ).fetchall()
    return [row["date"] for row in rows]


def add_free_date(target: date, weekday: str) -> None:
    target_str = target.strftime(DATE_FMT)
    date_iso   = target.strftime("%Y-%m-%d")
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO free_dates (date, date_iso, weekday) VALUES (?,?,?)",
            (target_str, date_iso, weekday)
        )


def remove_free_date(target: date) -> None:
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        con.execute("DELETE FROM free_dates WHERE date = ?", (target_str,))


# ─── Лиды ─────────────────────────────────────────────────────────────────────

def find_lead_by_phone(phone: str) -> dict | None:
    """Ищет лид по нормализованному номеру телефона. Возвращает dict или None."""
    if not phone:
        return None
    norm = normalize_phone(phone)
    if not norm:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM leads WHERE phone_norm = ?", (norm,)
        ).fetchone()
    return dict(row) if row else None


def add_lead(
    name: str,
    phone: str = "",
    nick: str = "",
    source: str = "Авито",
) -> bool:
    """
    Добавляет лид в SQLite.
    Возвращает False если дубль по телефону (и обновляет ник).
    """
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if phone:
        existing = find_lead_by_phone(phone)
        if existing:
            # Дубль — обновляем ник если пришёл новый
            if nick and not existing["nick"]:
                with _conn() as con:
                    con.execute(
                        "UPDATE leads SET nick = ? WHERE id = ?",
                        (nick, existing["id"])
                    )
            return False  # дубль
    with _conn() as con:
        con.execute(
            "INSERT INTO leads (datetime, name, phone, phone_norm, nick, source, synced_at) VALUES (?,?,?,?,?,?,?)",
            (now, name, phone, normalize_phone(phone), nick, source, now)
        )
    return True


# ─── Разрешённые пользователи ─────────────────────────────────────────────────

def get_allowed_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM allowed_users ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def is_allowed_user(telegram_id: int) -> bool:
    if not telegram_id:
        return False
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM allowed_users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row is not None


def add_allowed_user(telegram_id: int, username: str) -> bool:
    """Добавляет пользователя. Возвращает False если уже есть."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    uname = username.lstrip("@")
    # Проверяем дубль по username
    with _conn() as con:
        existing = con.execute(
            "SELECT id FROM allowed_users WHERE username = ?", (uname,)
        ).fetchone()
        if existing:
            return False
        # telegram_id=None пока пользователь не написал боту
        tid = telegram_id if telegram_id else None
        con.execute(
            "INSERT INTO allowed_users (telegram_id, username, added_at) VALUES (?,?,?)",
            (tid, uname, now)
        )
    return True


def remove_allowed_user(row_id: int) -> bool:
    """Удаляет пользователя по row id (работает даже если telegram_id ещё NULL)."""
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM allowed_users WHERE id = ?", (row_id,)
        )
    return cur.rowcount > 0


def update_allowed_user_id(username: str, telegram_id: int) -> None:
    """Обновляет telegram_id по username (когда пользователь впервые пишет боту)."""
    username = username.lstrip("@")
    with _conn() as con:
        con.execute(
            "UPDATE allowed_users SET telegram_id = ? WHERE username = ? AND (telegram_id IS NULL OR telegram_id = 0)",
            (telegram_id, username)
        )


# ─── Лог доступа ──────────────────────────────────────────────────────────────

def log_access(telegram_id: int, username: str, action: str) -> None:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute(
            "INSERT INTO access_log (telegram_id, username, action, timestamp) VALUES (?,?,?,?)",
            (telegram_id, username, action, now)
        )


def get_editor_ids(date_str: str) -> list[int]:
    """telegram_id всех, кто создавал или редактировал бронь на эту дату."""
    with _conn() as con:
        rows = con.execute(
            """SELECT DISTINCT telegram_id FROM access_log
               WHERE (action = ? OR action = ?) AND telegram_id IS NOT NULL AND telegram_id != 0""",
            (f"create:{date_str}", f"edit:{date_str}"),
        ).fetchall()
    return [r["telegram_id"] for r in rows]


def get_access_log(limit: int = 100) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Seen users (авто-приветствие) ───────────────────────────────────────────

def has_seen_user(telegram_id: int) -> bool:
    """True если пользователь уже получал приветствие."""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM seen_users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row is not None


def mark_user_seen(telegram_id: int) -> None:
    """Отмечает пользователя как приветствованного."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO seen_users (telegram_id, seen_at) VALUES (?,?)",
            (telegram_id, now),
        )


# ─── Avito chat state ─────────────────────────────────────────────────────────

def get_all_avito_chat_states() -> list[dict]:
    """Загружает все состояния Авито-чатов из SQLite (для старта поллера)."""
    with _conn() as con:
        rows = con.execute("SELECT * FROM avito_chat_state").fetchall()
    return [dict(r) for r in rows]


def upsert_avito_chat_state(
    chat_id: str,
    *,
    greeted:          bool = False,
    awaiting_contact: bool = False,
    asked_date:       bool = False,
    lead_received:    bool = False,
    offered_alt:      bool = False,
    faq_count:        int  = 0,
    stuck_notified:   bool = False,
    ignored:          bool = False,
    last_handled_id:  str  = "",
    ctx_date:         str  = "",
    ctx_guests:       str  = "",
    ctx_event:        str  = "",
    ctx_phone:        str  = "",
) -> None:
    """Сохраняет (INSERT OR UPDATE) состояние одного Авито-чата."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute("""
            INSERT INTO avito_chat_state
            (chat_id, greeted, awaiting_contact, asked_date, lead_received,
             offered_alt, faq_count, stuck_notified, ignored,
             last_handled_id, ctx_date, ctx_guests, ctx_event, ctx_phone, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                greeted=excluded.greeted,
                awaiting_contact=excluded.awaiting_contact,
                asked_date=excluded.asked_date,
                lead_received=excluded.lead_received,
                offered_alt=excluded.offered_alt,
                faq_count=excluded.faq_count,
                stuck_notified=excluded.stuck_notified,
                ignored=excluded.ignored,
                last_handled_id=excluded.last_handled_id,
                ctx_date=excluded.ctx_date,
                ctx_guests=excluded.ctx_guests,
                ctx_event=excluded.ctx_event,
                ctx_phone=excluded.ctx_phone,
                updated_at=excluded.updated_at
        """, (
            chat_id,
            int(greeted), int(awaiting_contact), int(asked_date),
            int(lead_received), int(offered_alt), faq_count,
            int(stuck_notified), int(ignored),
            last_handled_id, ctx_date, ctx_guests, ctx_event, ctx_phone,
            now,
        ))


# ─── Дополнительные запросы (используются в api.py) ──────────────────────────

def get_distinct_sources() -> list[str]:
    """Уникальные источники бронирований (для фильтра в статистике)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT source FROM bookings WHERE source != '' ORDER BY source"
        ).fetchall()
    return [row["source"] for row in rows]


def get_admin_summary() -> dict:
    """Сводная статистика для суперадмина."""
    from datetime import date as _date
    bookings = get_all_bookings()
    today = _date.today()
    future = [b for b in bookings if b["date_obj"] >= today]
    past   = [b for b in bookings if b["date_obj"] < today]
    total_guests = sum(
        int(b["guests"]) for b in bookings
        if b["guests"] and b["guests"].isdigit()
    )
    with _conn() as con:
        leads_count = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        users_count = con.execute("SELECT COUNT(*) FROM allowed_users").fetchone()[0]
        action_rows = con.execute("""
            SELECT username, COUNT(*) as cnt
            FROM access_log
            WHERE username != ''
            GROUP BY username
            ORDER BY cnt DESC
        """).fetchall()
    manager_actions = [{"username": r["username"], "count": r["cnt"]} for r in action_rows]
    return {
        "bookings_total":  len(bookings),
        "bookings_future": len(future),
        "bookings_past":   len(past),
        "guests_total":    total_guests,
        "leads_total":     leads_count,
        "managers_count":  users_count,
        "manager_actions": manager_actions,
    }
