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

log = logging.getLogger("DB")

DB_PATH  = os.path.join(os.path.dirname(__file__), "database.db")
DATE_FMT = "%d.%m.%Y"


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
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                date         TEXT PRIMARY KEY,   -- дд.мм.гггг
                guests       TEXT DEFAULT '',
                name         TEXT DEFAULT '',
                phone        TEXT DEFAULT '',
                source       TEXT DEFAULT '',
                client_type  TEXT DEFAULT '',
                comment      TEXT DEFAULT '',
                weekday      TEXT DEFAULT '',
                changed_by   TEXT DEFAULT '',
                changed_at   TEXT DEFAULT '',
                synced_at    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS free_dates (
                date         TEXT PRIMARY KEY,   -- дд.мм.гггг
                weekday      TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS leads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                datetime     TEXT,
                name         TEXT DEFAULT '',
                phone        TEXT DEFAULT '',
                nick         TEXT DEFAULT '',
                source       TEXT DEFAULT '',
                synced_at    TEXT DEFAULT ''
            );
        """)
    log.info("✅ SQLite инициализирована: %s", DB_PATH)


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
        with _conn() as con:
            con.execute("DELETE FROM bookings")
            for b in bookings:
                con.execute("""
                    INSERT INTO bookings
                    (date, guests, name, phone, source, client_type, comment, weekday, changed_by, changed_at, synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (b["date"], b["guests"], b["name"], b["phone"],
                      b["source"], b["client_type"], b["comment"],
                      b["weekday"], "", "", now))
        log.info("✅ Синхронизировано бронирований: %d", len(bookings))

        # Свободные даты
        free = get_free_dates(limit=9999)
        with _conn() as con:
            con.execute("DELETE FROM free_dates")
            for d_str in free:
                try:
                    d = datetime.strptime(d_str, DATE_FMT).date()
                    from sheets import _weekday
                    con.execute("INSERT INTO free_dates (date, weekday) VALUES (?,?)",
                                (d_str, _weekday(d)))
                except ValueError:
                    pass
        log.info("✅ Синхронизировано свободных дат: %d", len(free))

        # Лиды
        ws = _sheet_leads()
        rows = _data_rows(ws)
        with _conn() as con:
            con.execute("DELETE FROM leads")
            for row in rows:
                con.execute("""
                    INSERT INTO leads (datetime, name, phone, nick, source, synced_at)
                    VALUES (?,?,?,?,?,?)
                """, (
                    row[0] if len(row) > 0 else "",
                    row[1] if len(row) > 1 else "",
                    row[2] if len(row) > 2 else "",
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
        return {
            "found":       True,
            "guests":      row["guests"],
            "name":        row["name"],
            "phone":       row["phone"],
            "source":      row["source"],
            "client_type": row["client_type"],
            "comment":     row["comment"],
            "weekday":     row["weekday"],
        }
    return {"found": False}


def get_all_bookings() -> list[dict]:
    """Все бронирования из SQLite, отсортированные по дате (сортировка через Python)."""
    today = date.today()
    with _conn() as con:
        rows = con.execute("SELECT * FROM bookings").fetchall()
    result = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], DATE_FMT).date()
            result.append({
                "date":        row["date"],
                "date_obj":    d,
                "guests":      row["guests"],
                "name":        row["name"],
                "phone":       row["phone"],
                "source":      row["source"],
                "client_type": row["client_type"],
                "comment":     row["comment"],
                "weekday":     row["weekday"],
                "future":      d >= today,
            })
        except ValueError:
            pass
    result.sort(key=lambda x: x["date_obj"])
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
) -> None:
    """Добавляет или обновляет бронь в SQLite."""
    target_str = target.strftime(DATE_FMT)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute("""
            INSERT INTO bookings
            (date, guests, name, phone, source, client_type, comment, weekday, changed_by, changed_at, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                guests=excluded.guests, name=excluded.name, phone=excluded.phone,
                source=excluded.source, client_type=excluded.client_type,
                comment=excluded.comment, weekday=excluded.weekday,
                changed_by=excluded.changed_by, changed_at=excluded.changed_at,
                synced_at=excluded.synced_at
        """, (target_str, guests, name, phone, source, client_type,
              comment, weekday, changed_by, now, now))


def delete_booking(target: date) -> bool:
    """Удаляет бронь из SQLite. Возвращает True если запись была."""
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        cur = con.execute("DELETE FROM bookings WHERE date = ?", (target_str,))
    return cur.rowcount > 0


def update_booking_fields(target: date, changed_by: str = "", **fields) -> bool:
    """Обновляет отдельные поля брони."""
    target_str = target.strftime(DATE_FMT)
    allowed = {"guests", "name", "phone", "source", "client_type", "comment"}
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
    """Ближайшие свободные даты из SQLite (сортировка и фильтр через Python, т.к. формат ДД.ММ.ГГГГ не сортируется лексикографически)."""
    today = date.today()
    with _conn() as con:
        rows = con.execute("SELECT date FROM free_dates").fetchall()
    parsed = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], DATE_FMT).date()
            if d >= today:
                parsed.append((d, row["date"]))
        except ValueError:
            pass
    parsed.sort(key=lambda x: x[0])
    return [d_str for _, d_str in parsed[:limit]]


def add_free_date(target: date, weekday: str) -> None:
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO free_dates (date, weekday) VALUES (?,?)",
            (target_str, weekday)
        )


def remove_free_date(target: date) -> None:
    target_str = target.strftime(DATE_FMT)
    with _conn() as con:
        con.execute("DELETE FROM free_dates WHERE date = ?", (target_str,))


# ─── Лиды ─────────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    import re
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    return digits


def find_lead_by_phone(phone: str) -> dict | None:
    """Ищет лид по телефону. Возвращает dict или None."""
    if not phone:
        return None
    norm = _normalize_phone(phone)
    with _conn() as con:
        rows = con.execute("SELECT * FROM leads WHERE phone != ''").fetchall()
    for row in rows:
        if _normalize_phone(row["phone"]) == norm:
            return dict(row)
    return None


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
            "INSERT INTO leads (datetime, name, phone, nick, source, synced_at) VALUES (?,?,?,?,?,?)",
            (now, name, phone, nick, source, now)
        )
    return True
