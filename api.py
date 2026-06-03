"""
api.py — FastAPI бэкенд для Telegram Mini App «Муза».

Эндпоинты:
  GET  /                        → webapp/index.html
  GET  /api/calendar?month=     → список дат с статусом (YYYY-MM)
  GET  /api/booking/{date}      → детали брони (дата: ДД.ММ.ГГГГ)
  POST /api/booking             → создать бронь
  PUT  /api/booking/{date}      → изменить бронь
  DELETE /api/booking/{date}    → отменить бронь
  GET  /api/sources             → уникальные источники из реальных броней
  GET  /api/stats               → статистика бронирований
  POST /api/sync                → принудительная синхронизация из Google Sheets

Аутентификация: заголовок X-Init-Data с initData от Telegram WebApp SDK.
Доступ только для пользователей из ADMIN_CHAT_ID.

Запуск:
  uvicorn api:app --host 0.0.0.0 --port 8001
"""

import calendar as _cal
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import database

load_dotenv()

log = logging.getLogger("MUZA_API")
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID", "45028744"))
# Оставляем для обратной совместимости, основная проверка — через БД
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_CHAT_ID", "0").split(",")
    if x.strip().isdigit()
}

DATE_FMT = "%d.%m.%Y"

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL  = [
    "Понедельник", "Вторник", "Среда",
    "Четверг", "Пятница", "Суббота", "Воскресенье",
]
MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

WEBAPP_DIR = Path(__file__).parent / "webapp"

# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Муза API", docs_url=None, redoc_url=None)

_ALLOWED_ORIGINS = [
    "https://web.telegram.org",
    "https://webk.telegram.org",
    "https://webz.telegram.org",
]
# Добавляем ngrok/кастомный домен из env если задан
_webapp_origin = os.getenv("WEBAPP_URL", "").rstrip("/")
if _webapp_origin:
    _ALLOWED_ORIGINS.append(_webapp_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Init-Data"],
)


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup() -> None:
    database.init_db()
    # Включаем WAL для лучшей конкурентной записи (бот + апи одновременно)
    try:
        import sqlite3
        con = sqlite3.connect(database.DB_PATH)
        con.execute("PRAGMA journal_mode=WAL")
        con.close()
    except Exception as e:
        log.warning("WAL mode error: %s", e)
    database.sync_from_sheets()
    log.info("✅ API готов")


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _verify_init_data(init_data: str) -> dict:
    """
    Проверяет подпись initData от Telegram WebApp.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash in initData")

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )

    # secret_key = HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "Invalid initData signature")

    auth_date = int(params.get("auth_date", 0))
    if time.time() - auth_date > 86400:  # 24 часа
        raise HTTPException(401, "initData expired")

    return json.loads(params.get("user", "{}"))


def _require_admin(x_init_data: str = Header(default=None, alias="x-init-data")) -> dict:
    if not x_init_data:
        raise HTTPException(401, "Missing X-Init-Data header")
    user = _verify_init_data(x_init_data)
    uid = user.get("id")
    if uid != SUPERADMIN_ID and not database.is_allowed_user(uid):
        log.warning("Unauthorized access attempt: user_id=%s", uid)
        raise HTTPException(403, "Admins only")
    # Логируем каждый вход в Mini App (раз в сессию достаточно, логируем здесь)
    return user


def _require_superadmin(x_init_data: str = Header(default=None, alias="x-init-data")) -> dict:
    if not x_init_data:
        raise HTTPException(401, "Missing X-Init-Data header")
    user = _verify_init_data(x_init_data)
    if user.get("id") != SUPERADMIN_ID:
        raise HTTPException(403, "Superadmin only")
    return user


# ─── Static ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index() -> FileResponse:
    index = WEBAPP_DIR / "index.html"
    if not index.exists():
        raise HTTPException(503, "webapp/index.html not found")
    return FileResponse(index, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


# ─── Calendar ─────────────────────────────────────────────────────────────────

@app.get("/api/calendar")
async def get_calendar(month: str = None, user: dict = Depends(_require_admin)):
    """Возвращает статус каждого дня месяца. month: YYYY-MM (по умолчанию текущий)."""
    if not month:
        today = date.today()
        month = today.strftime("%Y-%m")

    try:
        year, mon = int(month[:4]), int(month[5:7])
        if not (1 <= mon <= 12):
            raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(400, "Invalid month format, use YYYY-MM")

    _, days_count = _cal.monthrange(year, mon)

    # Загружаем данные из SQLite (быстро)
    all_bookings = database.get_all_bookings()
    booked_map   = {b["date"]: b for b in all_bookings}
    free_set     = set(database.get_free_dates(limit=9999))

    today_date = date.today()
    days = []

    for day_num in range(1, days_count + 1):
        d     = date(year, mon, day_num)
        d_str = d.strftime(DATE_FMT)

        entry: dict = {
            "date":          d_str,
            "day":           day_num,
            "weekday_short": WEEKDAYS_SHORT[d.weekday()],
            "weekday":       WEEKDAYS_FULL[d.weekday()],
            "past":          d < today_date,
            "today":         d == today_date,
        }

        if d_str in booked_map:
            b = booked_map[d_str]
            entry["status"]      = "booked"
            entry["name"]        = b.get("name", "")
            entry["guests"]      = b.get("guests", "")
            entry["phone"]       = b.get("phone", "")
            entry["source"]      = b.get("source", "")
            entry["client_type"] = b.get("client_type", "")
            entry["comment"]     = b.get("comment", "")
            entry["tg_nick"]     = b.get("tg_nick", "")
        elif d_str in free_set:
            entry["status"] = "free"
        else:
            entry["status"] = "neutral"

        days.append(entry)

    first_weekday = date(year, mon, 1).weekday()  # 0=Пн, 6=Вс

    return {
        "month":        month,
        "year":         year,
        "mon":          mon,
        "month_name":   MONTH_NAMES[mon - 1],
        "first_weekday": first_weekday,
        "days":         days,
    }


# ─── Booking detail ───────────────────────────────────────────────────────────

@app.get("/api/booking/{date_str}")
async def get_booking(date_str: str, user: dict = Depends(_require_admin)):
    d = _parse_date(date_str)
    result = database.check_date(d)
    if not result["found"]:
        raise HTTPException(404, "Booking not found")
    result["date"] = date_str
    return result


# ─── Schemas ──────────────────────────────────────────────────────────────────

class BookingIn(BaseModel):
    date:        str
    guests:      str = ""
    name:        str = ""
    phone:       str = ""
    source:      str = ""
    client_type: str = ""
    comment:     str = ""
    tg_nick:     str = ""


# ─── Create booking ───────────────────────────────────────────────────────────

@app.post("/api/booking", status_code=201)
async def create_booking(body: BookingIn, user: dict = Depends(_require_admin)):
    d = _parse_date(body.date)

    if database.check_date(d)["found"]:
        raise HTTPException(409, "Date already booked")

    weekday  = WEEKDAYS_FULL[d.weekday()]
    username = _username(user)

    # sheets.add_booking() внутри уже пишет в SQLite — дублировать не нужно
    _sheets_write("add", d=d, guests=body.guests, name=body.name,
                  phone=body.phone, source=body.source,
                  client_type=body.client_type, comment=body.comment,
                  tg_nick=body.tg_nick, changed_by=username)

    database.log_access(user.get("id", 0), username, f"create:{body.date}")
    log.info("✅ Бронь создана: %s  (%s, by %s)", body.date, body.name, username)
    return {"ok": True, "date": body.date}


# ─── Update booking ───────────────────────────────────────────────────────────

@app.put("/api/booking/{date_str}")
async def update_booking(date_str: str, body: BookingIn, user: dict = Depends(_require_admin)):
    d = _parse_date(date_str)

    if not database.check_date(d)["found"]:
        raise HTTPException(404, "Booking not found")

    username = _username(user)

    # sheets.edit_booking() внутри уже пишет в SQLite — дублировать не нужно
    _sheets_write("edit", d=d, guests=body.guests, name=body.name,
                  phone=body.phone, source=body.source,
                  client_type=body.client_type, comment=body.comment,
                  tg_nick=body.tg_nick, changed_by=username)

    database.log_access(user.get("id", 0), username, f"edit:{date_str}")
    log.info("✏️  Бронь изменена: %s  (by %s)", date_str, username)
    return {"ok": True, "date": date_str}


# ─── Delete booking ───────────────────────────────────────────────────────────

@app.delete("/api/booking/{date_str}")
async def delete_booking_endpoint(date_str: str, user: dict = Depends(_require_admin)):
    d = _parse_date(date_str)

    if not database.check_date(d)["found"]:
        raise HTTPException(404, "Booking not found")

    username = _username(user)

    # sheets.remove_booking() внутри уже пишет в SQLite — дублировать не нужно
    _sheets_write("remove", d=d, changed_by=username)

    database.log_access(user.get("id", 0), username, f"delete:{date_str}")
    log.info("🗑  Бронь отменена: %s  (by %s)", date_str, username)
    return {"ok": True, "date": date_str}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> date:
    try:
        return datetime.strptime(date_str, DATE_FMT).date()
    except ValueError:
        raise HTTPException(400, f"Invalid date format '{date_str}', use DD.MM.YYYY")


def _username(user: dict) -> str:
    return user.get("username") or user.get("first_name") or "miniapp"


def _sheets_write(action: str, d: date, changed_by: str = "", **kwargs) -> None:
    """Пишет изменение в Google Sheets. При ошибке бросает HTTPException 502."""
    try:
        from sheets import add_booking, remove_booking, edit_booking
        if action == "add":
            add_booking(
                target=d,
                guests=kwargs.get("guests", ""),
                name=kwargs.get("name", ""),
                phone=kwargs.get("phone", ""),
                source=kwargs.get("source", ""),
                client_type=kwargs.get("client_type", ""),
                comment=kwargs.get("comment", ""),
                changed_by=changed_by,
                tg_nick=kwargs.get("tg_nick", ""),
            )
        elif action == "edit":
            edit_booking(
                target=d,
                changed_by=changed_by,
                guests=kwargs.get("guests", ""),
                name=kwargs.get("name", ""),
                phone=kwargs.get("phone", ""),
                source=kwargs.get("source", ""),
                client_type=kwargs.get("client_type", ""),
                comment=kwargs.get("comment", ""),
                tg_nick=kwargs.get("tg_nick", ""),
            )
        elif action == "remove":
            remove_booking(target=d)
    except Exception as e:
        log.error("Sheets error (%s): %s", action, e)
        raise HTTPException(502, f"Google Sheets error: {e}")


# ─── Sources ──────────────────────────────────────────────────────────────────

@app.get("/api/sources")
async def get_sources(user: dict = Depends(_require_admin)):
    """Уникальные источники из реальных броней в базе."""
    try:
        import sqlite3
        con = sqlite3.connect(database.DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT DISTINCT source FROM bookings WHERE source != '' ORDER BY source"
        ).fetchall()
        con.close()
        return [row["source"] for row in rows]
    except Exception:
        return []


# ─── Stats ────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats(source: str = None, month: str = None, user: dict = Depends(_require_admin)):
    """Статистика бронирований. ?source= и ?month=MM.YYYY фильтруют по источнику/месяцу."""
    from collections import Counter, defaultdict
    from datetime import date as date_cls

    today  = date_cls.today()
    if today.month == 12:
        next_m, next_y = 1, today.year + 1
    else:
        next_m, next_y = today.month + 1, today.year

    all_bk = database.get_all_bookings()

    def _month_key(d_str: str) -> str:
        """'15.06.2026' → '06.2026'"""
        return d_str[3:10]

    # Фильтр только по месяцу (для графика источников)
    month_only = all_bk
    if month:
        month_only = [b for b in all_bk if _month_key(b["date"]) == month]

    # Фильтр только по источнику (для графика месяцев)
    source_only = all_bk
    if source:
        source_only = [b for b in all_bk if (b.get("source") or "") == source]

    # Полный фильтр (для total, future, this_month, next_month)
    filtered = month_only
    if source:
        filtered = [b for b in filtered if (b.get("source") or "") == source]

    this_month_key = today.strftime("%m.%Y")
    next_month_key = f"{next_m:02d}.{next_y}"

    this_month_bk = [b for b in filtered if _month_key(b["date"]) == this_month_key]
    next_month_bk = [b for b in filtered if _month_key(b["date"]) == next_month_key]
    future        = [b for b in filtered if b.get("future")]

    # По источникам — в рамках выбранного месяца (если задан)
    source_counts = Counter(b.get("source") or "Не указан" for b in month_only)

    # Гости по источникам
    def _guests(b: dict) -> int:
        try: return int(b.get("guests") or 0)
        except: return 0

    source_guests: dict = defaultdict(int)
    for b in month_only:
        source_guests[b.get("source") or "Не указан"] += _guests(b)

    total_guests = sum(_guests(b) for b in filtered)

    # По месяцам — в рамках выбранного источника (если задан)
    by_month: dict = defaultdict(int)
    for b in source_only:
        try:
            d = b["date_obj"]
            key = f"{d.month:02d}.{d.year}"
            by_month[key] += 1
        except Exception:
            pass

    month_labels, month_counts, month_keys = [], [], []
    for delta in range(-12, 4):
        m = today.month + delta
        y = today.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        key = f"{m:02d}.{y}"
        cnt = by_month.get(key, 0)
        # Прошлые 12 + текущий — всегда; будущие — только с данными
        if delta <= 0 or cnt > 0:
            month_labels.append(MONTH_NAMES[m - 1][:3] + f" {y}")
            month_counts.append(cnt)
            month_keys.append(key)

    by_source_full = {
        src: {"count": cnt, "guests": source_guests.get(src, 0)}
        for src, cnt in source_counts.most_common(30)
    }

    return {
        "total":        len(filtered),
        "total_guests": total_guests,
        "future":       len(future),
        "this_month":   len(this_month_bk),
        "next_month":   len(next_month_bk),
        "by_source":    by_source_full,
        "by_month":     {"labels": month_labels, "counts": month_counts, "keys": month_keys},
        "this_month_name": MONTH_NAMES[today.month - 1],
        "next_month_name": MONTH_NAMES[next_m - 1],
        "active_source": source or "",
        "active_month":  month or "",
    }


# ─── Список всех бронирований ────────────────────────────────────────────────

@app.get("/api/bookings")
async def get_bookings(user: dict = Depends(_require_admin)):
    """Все бронирования, отсортированные по дате (для вкладки «Список»)."""
    bookings = database.get_all_bookings()
    return [
        {
            "date":        b["date"],
            "weekday":     b["weekday"],
            "name":        b["name"],
            "phone":       b["phone"],
            "guests":      b["guests"],
            "source":      b["source"],
            "client_type": b["client_type"],
            "comment":     b["comment"],
            "tg_nick":     b.get("tg_nick", ""),
            "future":      b["future"],
        }
        for b in bookings
    ]


# ─── Force sync ───────────────────────────────────────────────────────────────

@app.post("/api/sync")
async def force_sync(user: dict = Depends(_require_admin)):
    """Принудительная синхронизация из Google Sheets → SQLite."""
    try:
        database.sync_from_sheets()
        log.info("🔄 Ручная синхронизация выполнена пользователем %s", _username(user))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Sync error: {e}")


# ─── Admin: управление пользователями ─────────────────────────────────────────

class UserIn(BaseModel):
    usernames: list[str]  # список @username


@app.get("/api/admin/users")
async def admin_get_users(user: dict = Depends(_require_superadmin)):
    return database.get_allowed_users()


@app.post("/api/admin/users")
async def admin_add_users(body: UserIn, user: dict = Depends(_require_superadmin)):
    added, dupes = [], []
    for raw in body.usernames:
        uname = raw.strip().lstrip("@")
        if not uname:
            continue
        ok = database.add_allowed_user(telegram_id=0, username=uname)
        (added if ok else dupes).append(uname)
    log.info("➕ Добавлены пользователи: %s (дубли: %s)", added, dupes)
    return {"added": added, "dupes": dupes}


@app.delete("/api/admin/users/{row_id}")
async def admin_remove_user(row_id: int, user: dict = Depends(_require_superadmin)):
    """Удаляет пользователя по row id (работает даже если telegram_id ещё NULL)."""
    ok = database.remove_allowed_user(row_id)
    if not ok:
        raise HTTPException(404, "User not found")
    return {"ok": True}


# ─── Admin: сводная статистика ────────────────────────────────────────────────

@app.get("/api/admin/summary")
async def admin_get_summary(user: dict = Depends(_require_superadmin)):
    from datetime import date as _date
    bookings = database.get_all_bookings()
    today = _date.today()
    future = [b for b in bookings if b["date_obj"] >= today]
    past   = [b for b in bookings if b["date_obj"] <  today]
    total_guests = sum(
        int(b["guests"]) for b in bookings
        if b["guests"] and b["guests"].isdigit()
    )
    with database._conn() as con:
        leads_count = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        users_count = con.execute("SELECT COUNT(*) FROM allowed_users").fetchone()[0]
        action_rows = con.execute("""
            SELECT username, COUNT(*) as cnt
            FROM access_log
            WHERE username != ''
            GROUP BY username
            ORDER BY cnt DESC
        """).fetchall()
    manager_actions = [{"username": r[0], "count": r[1]} for r in action_rows]
    return {
        "bookings_total":   len(bookings),
        "bookings_future":  len(future),
        "bookings_past":    len(past),
        "guests_total":     total_guests,
        "leads_total":      leads_count,
        "managers_count":   users_count,
        "manager_actions":  manager_actions,
    }
