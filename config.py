"""
config.py — общие настройки проекта.

Единственное место где читаются переменные окружения для bot.py, api.py,
avito_poll.py и sheets.py. Импортировать отсюда, не из os.getenv напрямую.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ─────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
SUPERADMIN_ID:      int = int(os.getenv("SUPERADMIN_ID", "45028744"))
NOTIFY_USERNAME:    str = os.getenv("NOTIFY_USERNAME", "@rimskiymedved")
WEBAPP_URL:         str = os.getenv("WEBAPP_URL", "")

# ─── Google Sheets ────────────────────────────────────────────────────────────

SPREADSHEET_ID:          str = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS_PATH: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
SHEET_NAME:              str = os.getenv("SHEET_NAME", "Бронирования")
FREE_SHEET_NAME:         str = os.getenv("FREE_SHEET_NAME", "Свободные")
LEADS_SHEET_NAME:        str = os.getenv("LEADS_SHEET_NAME", "Авито")

# ─── Авито ────────────────────────────────────────────────────────────────────

AVITO_CLIENT_ID:     str = os.getenv("AVITO_CLIENT_ID", "")
AVITO_CLIENT_SECRET: str = os.getenv("AVITO_CLIENT_SECRET", "")
AVITO_ACCOUNT_NAME:  str = os.getenv("AVITO_ACCOUNT_NAME", "Муза")
AVITO_NOTIFY_GROUP:  int = int(os.getenv("AVITO_NOTIFY_GROUP_ID", "0"))

# ─── Авито-поллер (тюнинг) ────────────────────────────────────────────────────

AVITO_POLL_INTERVAL:               int = int(os.getenv("AVITO_POLL_INTERVAL", "20"))
AVITO_POLL_STALE_CHECK_EVERY:      int = int(os.getenv("AVITO_POLL_STALE_CHECK_EVERY", "2"))
AVITO_POLL_BROAD_CHATS_EVERY:      int = int(os.getenv("AVITO_POLL_BROAD_CHATS_EVERY", "3"))
AVITO_POLL_BROAD_CHATS_LIMIT:      int = int(os.getenv("AVITO_POLL_BROAD_CHATS_LIMIT", "10"))
AVITO_POLL_MESSAGES_LIMIT:         int = int(os.getenv("AVITO_POLL_MESSAGES_LIMIT", "100"))
AVITO_POLL_MAX_INBOUND_BURST:      int = int(os.getenv("AVITO_POLL_MAX_INBOUND_BURST", "15"))
AVITO_POLL_RECOVERY_PENDING_LIMIT: int = int(os.getenv("AVITO_POLL_RECOVERY_PENDING_LIMIT", "3"))

# ─── Мониторинг / health ──────────────────────────────────────────────────────
# Healthchecks.io: уникальный ping-URL. Если пинги прекратятся — сервис пришлёт тревогу.
HEALTHCHECK_URL:            str = os.getenv("HEALTHCHECK_URL", "")
HEALTHCHECK_PING_INTERVAL:  int = int(os.getenv("HEALTHCHECK_PING_INTERVAL", "300"))  # сек
# Час ежедневного отчёта «я жив» админу (0–23, по времени сервера)
HEARTBEAT_HOUR:             int = int(os.getenv("HEARTBEAT_HOUR", "10"))
