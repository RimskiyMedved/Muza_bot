"""
api.py — FastAPI бэкенд для Telegram Mini App «Муза».

Эндпоинты:
  GET  /                        → webapp/index.html
  GET  /api/calendar?month=     → список дат с статусом (YYYY-MM)
  GET  /api/booking/{date}      → детали брони (дата: ДД.ММ.ГГГГ)
  POST /api/booking             → создать бронь
  PUT  /api/booking/{date}      → изменить бронь
  POST /api/booking/{date}/notify-mismatch → уведомить менеджеров о расхождении оплат
  DELETE /api/booking/{date}    → отменить бронь
  GET  /api/sources             → уникальные источники из реальных броней
  GET  /api/stats               → статистика бронирований
  GET  /api/settings            → настройки ставок (только для админа)
  PUT  /api/settings/{key}      → обновить одну настройку
  POST /api/sync                → принудительная синхронизация из Google Sheets

Аутентификация: заголовок X-Init-Data с initData от Telegram WebApp SDK.
Доступ только для пользователей из ADMIN_CHAT_ID.

Запуск:
  uvicorn api:app --host 0.0.0.0 --port 8001
"""

import asyncio
import calendar as _cal
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import database

from config import TELEGRAM_BOT_TOKEN, SUPERADMIN_ID, WEBAPP_URL as _WEBAPP_URL

log = logging.getLogger("MUZA_API")
logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)

BOT_TOKEN = TELEGRAM_BOT_TOKEN

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


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()          # init_db уже включает WAL внутри себя
    database.sync_from_sheets()
    log.info("✅ API готов")
    yield                       # приложение работает
    # (здесь можно закрыть ресурсы при shutdown)


# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Муза API",
    docs_url=None, redoc_url=None, openapi_url=None,  # схема API не отдаётся наружу
    lifespan=lifespan,
)

# ─── Rate limiting ──────────────────────────────────────────────────────────────
# За Caddy реальный IP приходит в X-Forwarded-For; берём первый адрес из цепочки.
def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)

limiter = Limiter(key_func=_client_ip, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Middleware применяет default_limits ко всем запросам ДО разбора зависимостей,
# то есть до проверки HMAC initData — анонимный флуд отсекается дёшево.
app.add_middleware(SlowAPIMiddleware)

_ALLOWED_ORIGINS = [
    "https://web.telegram.org",
    "https://webk.telegram.org",
    "https://webz.telegram.org",
]
# Добавляем ngrok/кастомный домен из env если задан
_webapp_origin = _WEBAPP_URL.rstrip("/")
if _webapp_origin:
    _ALLOWED_ORIGINS.append(_webapp_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Init-Data"],
)


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


# ─── Health (для внешнего мониторинга: UptimeRobot и т.п.) ────────────────────

HEALTH_MAX_AGE_SEC = 300  # бот считается живым, если heartbeat не старше 5 минут

@app.get("/health")
@limiter.exempt
async def health(request: Request) -> JSONResponse:
    """
    Публичный health-check. 200 — API жив и бот пишет heartbeat не старше 5 мин.
    503 — бот завис/мёртв (heartbeat устарел или отсутствует). Авторизация не нужна.
    """
    hb = database.get_bot_heartbeat()
    age = None
    bot_ok = False
    if hb:
        try:
            age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
            bot_ok = age < HEALTH_MAX_AGE_SEC
        except (ValueError, TypeError):
            bot_ok = False
    return JSONResponse(
        {
            "status": "ok" if bot_ok else "stale",
            "api": "up",
            "bot": "up" if bot_ok else "down",
            "bot_heartbeat_age_sec": int(age) if age is not None else None,
        },
        status_code=200 if bot_ok else 503,
    )


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
    # Добавляем вычисленные финансы (прибыль, расходы по статьям)
    fin = database.compute_financials(result)
    result.update(fin)
    return result


# ─── Schemas ──────────────────────────────────────────────────────────────────

class BookingIn(BaseModel):
    date:              str
    guests:            str   = ""
    name:              str   = ""
    phone:             str   = ""
    source:            str   = ""
    client_type:       str   = ""
    comment:           str   = ""
    contract_date:     str   = ""
    revenue_rent:      float = 0
    revenue_menu:      float = 0
    paid_advance:      float = 0
    paid_rent:         float = 0
    paid_final:        float = 0
    staff_waiters:     int   = 0
    staff_cooks:       int   = 0
    staff_cleaning:    int   = 0
    paid_advance_date:      str   = ""
    paid_rent_date:         str   = ""
    paid_final_date:        str   = ""
    cost_laundry:           float = 0
    cost_purchase:          float = 0
    cost_purchase_comment:  str   = ""
    cost_extra:             float = 0
    cost_extra_comment:     str   = ""
    has_manager:            int   = 1
    has_chef:               int   = 1
    has_assistant:          int   = 1
    menu_url:               str   = ""


class SettingIn(BaseModel):
    value: float


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
                  contract_date=body.contract_date,
                  revenue_rent=body.revenue_rent, revenue_menu=body.revenue_menu,
                  paid_advance=body.paid_advance, paid_rent=body.paid_rent,
                  paid_final=body.paid_final,
                  staff_waiters=body.staff_waiters, staff_cooks=body.staff_cooks,
                  staff_cleaning=body.staff_cleaning,
                  paid_advance_date=body.paid_advance_date,
                  paid_rent_date=body.paid_rent_date,
                  paid_final_date=body.paid_final_date,
                  cost_laundry=body.cost_laundry,
                  cost_purchase=body.cost_purchase,
                  cost_purchase_comment=body.cost_purchase_comment,
                  cost_extra=body.cost_extra,
                  cost_extra_comment=body.cost_extra_comment,
                  has_manager=body.has_manager,
                  has_chef=body.has_chef,
                  has_assistant=body.has_assistant,
                  menu_url=body.menu_url,
                  changed_by=username)

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
                  contract_date=body.contract_date,
                  revenue_rent=body.revenue_rent, revenue_menu=body.revenue_menu,
                  paid_advance=body.paid_advance, paid_rent=body.paid_rent,
                  paid_final=body.paid_final,
                  staff_waiters=body.staff_waiters, staff_cooks=body.staff_cooks,
                  staff_cleaning=body.staff_cleaning,
                  paid_advance_date=body.paid_advance_date,
                  paid_rent_date=body.paid_rent_date,
                  paid_final_date=body.paid_final_date,
                  cost_laundry=body.cost_laundry,
                  cost_purchase=body.cost_purchase,
                  cost_purchase_comment=body.cost_purchase_comment,
                  cost_extra=body.cost_extra,
                  cost_extra_comment=body.cost_extra_comment,
                  has_manager=body.has_manager,
                  has_chef=body.has_chef,
                  has_assistant=body.has_assistant,
                  menu_url=body.menu_url,
                  changed_by=username)

    database.log_access(user.get("id", 0), username, f"edit:{date_str}")
    log.info("✏️  Бронь изменена: %s  (by %s)", date_str, username)
    return {"ok": True, "date": date_str}


# ─── Payment mismatch notification ───────────────────────────────────────────

async def _tg_send(chat_id: int, text: str) -> None:
    """Отправляет сообщение через Telegram Bot API (fire-and-forget)."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception as exc:
        log.warning("tg_send failed for %s: %s", chat_id, exc)


@app.post("/api/booking/{date_str}/notify-mismatch")
async def notify_payment_mismatch(
    date_str: str,
    user: dict = Depends(_require_admin),
):
    d = _parse_date(date_str)
    result = database.check_date(d)
    if not result["found"]:
        raise HTTPException(404, "Booking not found")

    total_revenue = float(result.get("revenue_rent") or 0) + float(result.get("revenue_menu") or 0)
    total_paid    = (float(result.get("paid_advance") or 0)
                   + float(result.get("paid_rent")    or 0)
                   + float(result.get("paid_final")   or 0))
    diff = round(total_revenue - total_paid, 2)

    editor_ids = database.get_editor_ids(date_str)
    if not editor_ids:
        # Запасной вариант — текущий пользователь
        editor_ids = [user.get("id", 0)]

    name = result.get("name", "—")
    def _fmt(n: float) -> str:
        return f"{int(n):,}".replace(",", " ") + " ₽"

    msg = (
        f"⚠️ <b>Расхождение в оплатах</b>\n"
        f"Мероприятие: <b>{date_str}</b> · {name}\n"
        f"Выручка: <b>{_fmt(total_revenue)}</b>\n"
        f"Оплачено: <b>{_fmt(total_paid)}</b>\n"
        f"Разница: <b>{'+' if diff >= 0 else ''}{_fmt(diff)}</b>\n\n"
        f"Пожалуйста, проверьте суммы в Mini App."
    )

    await asyncio.gather(*[_tg_send(tid, msg) for tid in editor_ids], return_exceptions=True)
    log.info("💬 Уведомление об оплатах отправлено: %s → %s", date_str, editor_ids)
    return {"ok": True, "notified": len(editor_ids)}


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
                contract_date=kwargs.get("contract_date", ""),
                revenue_rent=kwargs.get("revenue_rent", 0),
                revenue_menu=kwargs.get("revenue_menu", 0),
                paid_advance=kwargs.get("paid_advance", 0),
                paid_rent=kwargs.get("paid_rent", 0),
                paid_final=kwargs.get("paid_final", 0),
                staff_waiters=kwargs.get("staff_waiters", 0),
                staff_cooks=kwargs.get("staff_cooks", 0),
                staff_cleaning=kwargs.get("staff_cleaning", 0),
                paid_advance_date=kwargs.get("paid_advance_date", ""),
                paid_rent_date=kwargs.get("paid_rent_date", ""),
                paid_final_date=kwargs.get("paid_final_date", ""),
                cost_laundry=kwargs.get("cost_laundry", 0),
                cost_purchase=kwargs.get("cost_purchase", 0),
                cost_purchase_comment=kwargs.get("cost_purchase_comment", ""),
                cost_extra=kwargs.get("cost_extra", 0),
                cost_extra_comment=kwargs.get("cost_extra_comment", ""),
                has_manager=kwargs.get("has_manager", 1),
                has_chef=kwargs.get("has_chef", 1),
                has_assistant=kwargs.get("has_assistant", 1),
                menu_url=kwargs.get("menu_url", ""),
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
                contract_date=kwargs.get("contract_date", ""),
                revenue_rent=kwargs.get("revenue_rent", 0),
                revenue_menu=kwargs.get("revenue_menu", 0),
                paid_advance=kwargs.get("paid_advance", 0),
                paid_rent=kwargs.get("paid_rent", 0),
                paid_final=kwargs.get("paid_final", 0),
                staff_waiters=kwargs.get("staff_waiters", 0),
                staff_cooks=kwargs.get("staff_cooks", 0),
                staff_cleaning=kwargs.get("staff_cleaning", 0),
                paid_advance_date=kwargs.get("paid_advance_date", ""),
                paid_rent_date=kwargs.get("paid_rent_date", ""),
                paid_final_date=kwargs.get("paid_final_date", ""),
                cost_laundry=kwargs.get("cost_laundry", 0),
                cost_purchase=kwargs.get("cost_purchase", 0),
                cost_purchase_comment=kwargs.get("cost_purchase_comment", ""),
                cost_extra=kwargs.get("cost_extra", 0),
                cost_extra_comment=kwargs.get("cost_extra_comment", ""),
                has_manager=kwargs.get("has_manager", 1),
                has_chef=kwargs.get("has_chef", 1),
                has_assistant=kwargs.get("has_assistant", 1),
                menu_url=kwargs.get("menu_url", ""),
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
        return database.get_distinct_sources()
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
    by_month: dict         = defaultdict(int)
    fin_by_month: dict     = defaultdict(lambda: {"income": 0.0, "expenses": 0.0, "profit": 0.0})
    for b in source_only:
        try:
            d = b["date_obj"]
            key = f"{d.month:02d}.{d.year}"
            by_month[key] += 1
            fin_by_month[key]["income"]   += float(b.get("total_income")   or 0)
            fin_by_month[key]["expenses"] += float(b.get("total_expenses") or 0)
            fin_by_month[key]["profit"]   += float(b.get("profit")         or 0)
        except Exception:
            pass

    month_labels, month_counts, month_keys = [], [], []
    month_incomes, month_expenses, month_profits = [], [], []
    for delta in range(-6, 7):
        m = today.month + delta
        y = today.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        key = f"{m:02d}.{y}"
        cnt = by_month.get(key, 0)
        # Прошлые — только если есть брони (не показываем пустые); текущий и будущие — всегда
        if delta >= 0 or cnt > 0:
            month_labels.append(MONTH_NAMES[m - 1][:3] + f" {y}")
            month_counts.append(cnt)
            month_keys.append(key)
            fin = fin_by_month.get(key, {})
            month_incomes.append(round(fin.get("income",   0), 2))
            month_expenses.append(round(fin.get("expenses", 0), 2))
            month_profits.append(round(fin.get("profit",   0), 2))

    # Выручка по источникам
    source_revenue: dict = defaultdict(float)
    for b in month_only:
        src = b.get("source") or "Не указан"
        source_revenue[src] += float(b.get("total_income") or 0)

    by_source_full = {
        src: {
            "count":   cnt,
            "guests":  source_guests.get(src, 0),
            "revenue": round(source_revenue.get(src, 0), 2),
        }
        for src, cnt in source_counts.most_common(30)
    }

    # Финансовые итоги по выбранному периоду
    fin_income   = round(sum(float(b.get("total_income")   or 0) for b in filtered), 2)
    fin_expenses = round(sum(float(b.get("total_expenses") or 0) for b in filtered), 2)
    fin_profit   = round(fin_income - fin_expenses, 2)

    # Средний чек
    fin_bookings = [b for b in filtered if float(b.get("total_income") or 0) > 0]
    avg_check = round(fin_income / len(fin_bookings)) if fin_bookings else 0

    # Средние гости
    guests_list = [_guests(b) for b in filtered if _guests(b) > 0]
    avg_guests = round(sum(guests_list) / len(guests_list)) if guests_list else 0

    # Долг и сбор оплат (только прошедшие банкеты)
    past_bk = [b for b in filtered if not b.get("future")]
    past_income = sum(float(b.get("total_income") or 0) for b in past_bk)
    total_paid  = sum(
        float(b.get("paid_advance") or 0) +
        float(b.get("paid_rent")    or 0) +
        float(b.get("paid_final")   or 0)
        for b in past_bk
    )
    total_debt      = round(max(0.0, past_income - total_paid), 2)
    collection_rate = round(total_paid / past_income * 100) if past_income > 0 else 100

    # Прирост к прошлому месяцу (для текущего месяца)
    if today.month == 1:
        prev_m2, prev_y2 = 12, today.year - 1
    else:
        prev_m2, prev_y2 = today.month - 1, today.year
    prev_month_key = f"{prev_m2:02d}.{prev_y2}"
    prev_month_cnt = len([b for b in source_only if _month_key(b["date"]) == prev_month_key])
    mom_change = len(this_month_bk) - prev_month_cnt

    # Загруженность (только при фильтре по месяцу)
    occupancy_pct = 0
    if month:
        import calendar as _cal
        try:
            mm2, yy2 = int(month[:2]), int(month[3:])
            days_in_month = _cal.monthrange(yy2, mm2)[1]
            booked_days   = len(set(b["date"] for b in filtered))
            occupancy_pct = round(booked_days / days_in_month * 100)
        except Exception:
            pass

    return {
        "total":        len(filtered),
        "total_guests": total_guests,
        "future":       len(future),
        "this_month":   len(this_month_bk),
        "next_month":   len(next_month_bk),
        "by_source":    by_source_full,
        "by_month":     {
            "labels":   month_labels,
            "counts":   month_counts,
            "keys":     month_keys,
            "incomes":  month_incomes,
            "expenses": month_expenses,
            "profits":  month_profits,
        },
        "fin_income":      fin_income,
        "fin_expenses":    fin_expenses,
        "fin_profit":      fin_profit,
        "avg_check":       avg_check,
        "avg_guests":      avg_guests,
        "total_debt":      total_debt,
        "collection_rate": collection_rate,
        "mom_change":      mom_change,
        "occupancy_pct":   occupancy_pct,
        "this_month_name": MONTH_NAMES[today.month - 1],
        "next_month_name": MONTH_NAMES[next_m - 1],
        "active_source":   source or "",
        "active_month":    month or "",
    }


# ─── Settings (admin) ────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(user: dict = Depends(_require_superadmin)):
    """Возвращает все настройки ставок (только суперадмин)."""
    settings = database.get_settings()
    return {
        k: {"value": v, "label": database.SETTINGS_LABELS.get(k, k)}
        for k, v in settings.items()
    }


@app.put("/api/settings/{key}")
async def update_setting(key: str, body: SettingIn, user: dict = Depends(_require_superadmin)):
    """Обновляет одну настройку (только суперадмин — ставки влияют на все расчёты)."""
    if body.value < 0:
        raise HTTPException(400, "Value must be non-negative")
    ok = database.update_setting(key, body.value)
    if not ok:
        raise HTTPException(404, f"Unknown setting key: {key}")
    username = _username(user)
    database.log_access(user.get("id", 0), username, f"settings:{key}={body.value}")
    log.info("⚙️  Настройка изменена: %s = %s  (by %s)", key, body.value, username)
    return {"ok": True, "key": key, "value": body.value}


# ─── Список всех бронирований ────────────────────────────────────────────────

@app.get("/api/bookings")
async def get_bookings(user: dict = Depends(_require_admin)):
    """Все бронирования, отсортированные по дате (для вкладки «Список»)."""
    bookings = database.get_all_bookings()
    result = []
    for b in bookings:
        total_income = float(b.get("revenue_rent") or 0) + float(b.get("revenue_menu") or 0)
        total_paid   = (float(b.get("paid_advance") or 0) +
                        float(b.get("paid_rent")    or 0) +
                        float(b.get("paid_final")   or 0))
        debt = round(total_income - total_paid, 2) if total_income > 0 else 0.0
        result.append({
            "date":         b["date"],
            "weekday":      b["weekday"],
            "name":         b["name"],
            "phone":        b["phone"],
            "guests":       b["guests"],
            "source":       b["source"],
            "client_type":  b["client_type"],
            "comment":      b["comment"],
            "future":       b["future"],
            # финансовое резюме для карточки
            "profit":         b.get("profit", 0),
            "total_income":   total_income,
            "total_expenses": b.get("total_expenses", 0),
            "has_financials": bool(total_income),
            "debt":           debt if debt > 0 else 0.0,
        })
    return result


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
    summary = database.get_admin_summary()
    # Groq usage
    usage = database.get_voice_usage(days=7)
    today_row = usage[0] if usage else {}
    summary["groq"] = {
        "today_transcriptions": today_row.get("transcriptions", 0),
        "today_audio_sec":      round(today_row.get("audio_seconds", 0)),
        "limit_audio_sec":      7200,
        "limit_transcriptions": 500,
        "week": usage,
    }
    return summary


# ─── Admin: аналитика (суперадмин) ────────────────────────────────────────────

@app.get("/api/admin/analytics")
async def admin_get_analytics(user: dict = Depends(_require_superadmin)):
    """Глубокая аналитика по дате мероприятия (по годам с 2026): действия + финансы."""
    from collections import defaultdict
    from datetime import date as date_cls

    actions = database.get_admin_action_analytics(activity_days=30)

    today = date_cls.today()
    START_YEAR = 2026

    def _mkey(b) -> str:
        d = b["date_obj"]
        return f"{d.month:02d}.{d.year}"

    # окно: все брони с START_YEAR и дальше (по дате мероприятия)
    win = [b for b in database.get_all_bookings() if b["date_obj"].year >= START_YEAR]

    # помесячный ряд: с января START_YEAR по последний месяц с данными (или текущий)
    last = max([b["date_obj"] for b in win], default=today)
    end_y, end_m = max((last.year, last.month), (today.year, today.month))
    months_keys = []
    yy, mm = START_YEAR, 1
    while (yy, mm) <= (end_y, end_m):
        months_keys.append((mm, yy))
        mm += 1
        if mm == 13:
            mm, yy = 1, yy + 1

    CATS = [
        ("Менеджер", "cost_manager"), ("Шеф", "cost_chef"), ("Помощник", "cost_assistant"),
        ("Официанты", "cost_waiters"), ("Повара", "cost_cooks"), ("Клининг", "cost_cleaning"),
        ("Прачка", "cost_laundry"), ("Закупка", "cost_purchase"), ("Доп. расходы", "cost_extra"),
        ("Агентские", "agency_fee"),
    ]

    fin_m  = {f"{mm:02d}.{yy}": {"income": 0.0, "expenses": 0.0, "profit": 0.0,
                                 "count": 0, "income_count": 0} for mm, yy in months_keys}
    struct = {field: 0.0 for _, field in CATS}
    occ    = {f"{mm:02d}.{yy}": {"days": set(), "wd": set(), "we": set()} for mm, yy in months_keys}
    src    = defaultdict(lambda: {"count": 0, "revenue": 0.0})
    ctypes = {"agency": {"count": 0, "revenue": 0.0, "profit": 0.0},
              "direct": {"count": 0, "revenue": 0.0, "profit": 0.0}}

    for b in win:
        k   = _mkey(b)
        inc = float(b.get("total_income")   or 0)
        exp = float(b.get("total_expenses") or 0)
        pro = float(b.get("profit")         or 0)
        fm = fin_m[k]
        fm["income"] += inc; fm["expenses"] += exp; fm["profit"] += pro; fm["count"] += 1
        if inc > 0:
            fm["income_count"] += 1
        for _, field in CATS:
            struct[field] += float(b.get(field) or 0)
        d = b["date_obj"]
        o = occ[k]; o["days"].add(b["date"])
        (o["we"] if d.weekday() >= 5 else o["wd"]).add(b["date"])
        s = b.get("source") or "Не указан"
        src[s]["count"] += 1; src[s]["revenue"] += inc
        ct = "agency" if (b.get("client_type") or "").strip() == "Агентство" else "direct"
        ctypes[ct]["count"] += 1; ctypes[ct]["revenue"] += inc; ctypes[ct]["profit"] += pro

    months = []
    prev_profit = None
    for mm, yy in months_keys:
        fm = fin_m[f"{mm:02d}.{yy}"]
        inc, exp, pro = fm["income"], fm["expenses"], fm["profit"]
        cnt, icnt = fm["count"], fm["income_count"]
        mom = None
        if prev_profit is not None and prev_profit != 0:
            mom = round((pro - prev_profit) / abs(prev_profit) * 100)
        prev_profit = pro
        days_in = _cal.monthrange(yy, mm)[1]
        o = occ[f"{mm:02d}.{yy}"]
        months.append({
            "key": f"{mm:02d}.{yy}", "label": MONTH_NAMES[mm - 1][:3] + f" {yy}",
            "income": round(inc), "expenses": round(exp), "profit": round(pro),
            "margin": round(pro / inc * 100, 1) if inc > 0 else 0,
            "count": cnt,
            "avg_check": round(inc / icnt) if icnt else 0,
            "avg_profit": round(pro / cnt) if cnt else 0,
            "mom_profit_pct": mom,
            "occ_pct": round(len(o["days"]) / days_in * 100),
            "booked": len(o["days"]), "weekday": len(o["wd"]), "weekend": len(o["we"]),
        })

    total_struct = sum(struct.values()) or 1
    expense_structure = sorted(
        [{"label": lbl, "amount": round(struct[field]),
          "pct": round(struct[field] / total_struct * 100, 1)}
         for lbl, field in CATS if struct[field] > 0],
        key=lambda x: x["amount"], reverse=True,
    )

    sources = sorted(
        [{"source": s, "count": v["count"], "revenue": round(v["revenue"]),
          "avg_check": round(v["revenue"] / v["count"]) if v["count"] else 0}
         for s, v in src.items()],
        key=lambda x: x["revenue"], reverse=True,
    )

    tot_inc  = sum(m["income"]   for m in months)
    tot_exp  = sum(m["expenses"] for m in months)
    tot_pro  = sum(m["profit"]   for m in months)
    tot_cnt  = sum(m["count"]    for m in months)
    tot_icnt = sum(1 for b in win if float(b.get("total_income") or 0) > 0)
    totals = {
        "income": tot_inc, "expenses": tot_exp, "profit": tot_pro,
        "margin": round(tot_pro / tot_inc * 100, 1) if tot_inc else 0,
        "count": tot_cnt,
        "avg_check": round(tot_inc / tot_icnt) if tot_icnt else 0,
        "avg_profit": round(tot_pro / tot_cnt) if tot_cnt else 0,
    }
    for ct in ctypes.values():
        ct["revenue"] = round(ct["revenue"]); ct["profit"] = round(ct["profit"])

    # ── По годам (с START_YEAR) ──
    years_map: dict = {}
    for b in win:
        yr = b["date_obj"].year
        ya = years_map.setdefault(yr, {"income": 0.0, "expenses": 0.0, "profit": 0.0,
                                       "count": 0, "income_count": 0, "days": set()})
        inc = float(b.get("total_income") or 0)
        ya["income"]   += inc
        ya["expenses"] += float(b.get("total_expenses") or 0)
        ya["profit"]   += float(b.get("profit") or 0)
        ya["count"]    += 1
        if inc > 0:
            ya["income_count"] += 1
        ya["days"].add(b["date"])
    years = []
    for yr in sorted(years_map):
        ya = years_map[yr]
        inc, pro, cnt, icnt = ya["income"], ya["profit"], ya["count"], ya["income_count"]
        years.append({
            "year": yr, "income": round(inc), "expenses": round(ya["expenses"]),
            "profit": round(pro), "margin": round(pro / inc * 100, 1) if inc else 0,
            "count": cnt, "booked": len(ya["days"]),
            "avg_check": round(inc / icnt) if icnt else 0,
            "avg_profit": round(pro / cnt) if cnt else 0,
        })

    return {
        "actions": actions,
        "finance": {
            "years": years,
            "months": months,
            "expense_structure": expense_structure,
            "sources": sources,
            "client_types": ctypes,
            "totals": totals,
        },
    }
