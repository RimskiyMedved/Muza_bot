"""
avito_export.py — экспорт диалогов Авито напрямую в Google Таблицу.

Запуск:
    python3 avito_export.py

Результат:
    Лист «Диалоги» в той же Google Таблице, что и бронирования.

Структура листа:
    A             B             C     D       E            F
    ID клиента    Имя клиента   Дата  Время   Кто пишет   Сообщение

Каждое сообщение — отдельная строка.
Строки одного клиента выделены одним цветом (чередующийся фон).
"""

import asyncio
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from avito import AvitoClient

BASE_URL = "https://api.avito.ru"
CHAT_LIMIT = int(os.getenv("EXPORT_CHAT_LIMIT", "500"))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
EXPORT_SHEET_NAME = os.getenv("EXPORT_SHEET_NAME", "Диалоги")

HEADERS = ["ID клиента", "Имя клиента", "Дата", "Время", "Кто пишет", "Сообщение"]

# Цвета для чередования строк клиентов (светло-голубой / белый)
COLOR_A = {"red": 0.878, "green": 0.933, "blue": 0.980}   # #E0EEFA
COLOR_B = {"red": 1.0,   "green": 1.0,   "blue": 1.0}     # белый

# Цвет заголовка
COLOR_HEADER = {"red": 0.204, "green": 0.396, "blue": 0.643}  # #345A80


# ─── Google Sheets ────────────────────────────────────────────────────────────

def _get_sheet() -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
        scopes=SCOPES,
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(os.getenv("SPREADSHEET_ID"))

    # Создаём лист если его нет
    try:
        ws = ss.worksheet(EXPORT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=EXPORT_SHEET_NAME, rows=10000, cols=6)
        print(f"  ✅ Создан новый лист «{EXPORT_SHEET_NAME}»")

    return ws


def _write_to_sheet(ws: gspread.Worksheet, rows: list[list], client_ids: list[str]) -> None:
    """Записывает данные, форматирует заголовок и красит строки."""
    total_rows = len(rows) + 1  # +1 заголовок

    # Расширяем лист если нужно
    if ws.row_count < total_rows + 10:
        ws.resize(rows=total_rows + 100)

    # Очищаем и пишем данные
    ws.clear()
    ws.update("A1", [HEADERS] + rows, value_input_option="USER_ENTERED")
    print(f"  📝 Записано {len(rows)} строк")

    # ── Форматирование ────────────────────────────────────────────────────────

    requests = []

    # Заголовок: жирный белый текст, тёмный фон
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 6,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": COLOR_HEADER,
                    "textFormat": {"bold": True, "foregroundColor": {"red":1,"green":1,"blue":1}},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    })

    # Закрепляем первую строку
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Ширина столбцов: ID, Имя, Дата, Время, Кто пишет, Сообщение
    col_widths = [130, 160, 90, 70, 140, 600]
    for col_idx, width in enumerate(col_widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex": col_idx + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    # Перенос текста в столбце «Сообщение» (F)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": 1, "endRowIndex": total_rows,
                "startColumnIndex": 5, "endColumnIndex": 6,
            },
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }
    })

    # Чередующийся цвет по клиенту
    seen_ids: dict[str, int] = {}   # client_id → порядковый номер (0 или 1)
    color_counter = 0
    for i, cid in enumerate(client_ids):
        if cid not in seen_ids:
            seen_ids[cid] = color_counter % 2
            color_counter += 1
        color = COLOR_A if seen_ids[cid] == 0 else COLOR_B
        row_idx = i + 1   # +1 заголовок, 0-based
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0, "endColumnIndex": 6,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    # Применяем всё одним batch-запросом
    ws.spreadsheet.batch_update({"requests": requests})
    print(f"  🎨 Форматирование применено")


# ─── Авито: загрузка чатов ───────────────────────────────────────────────────

async def get_all_chats(client: AvitoClient) -> list[dict]:
    await client._ensure_token()
    uid = await client.get_user_id()

    all_chats = []
    offset_id = None
    page = 1

    while True:
        params = {"unread_only": "false", "chat_types": "u2i", "limit": 100}
        if offset_id:
            params["offset_id"] = offset_id

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as http:
                    resp = await http.get(
                        f"{BASE_URL}/messenger/v2/accounts/{uid}/chats",
                        headers=client._auth(), params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    print(f"\n  ⚠️  Попытка {attempt+1} не удалась ({e.__class__.__name__}), повтор...")
                    await asyncio.sleep(3)
                else:
                    raise

        chats = data.get("chats", [])
        if not chats:
            break

        all_chats.extend(chats)

        if len(all_chats) >= CHAT_LIMIT:
            all_chats = all_chats[:CHAT_LIMIT]
            print(f"  Загружено {len(all_chats)} чатов  ← лимит {CHAT_LIMIT}")
            break

        print(f"  Страница {page}: {len(all_chats)} чатов...", end="\r", flush=True)

        if len(chats) < 100:
            print(f"  Загружено {len(all_chats)} чатов  ← больше нет")
            break

        offset_id = chats[-1].get("id")
        page += 1
        await asyncio.sleep(0.5)

    return all_chats


async def get_all_messages(client: AvitoClient, chat_id: str) -> list[dict]:
    await client._ensure_token()
    uid = await client.get_user_id()

    all_msgs = []
    offset_id = None

    while True:
        params = {"limit": 100}
        if offset_id:
            params["offset_id"] = offset_id

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as http:
                    resp = await http.get(
                        f"{BASE_URL}/messenger/v3/accounts/{uid}/chats/{chat_id}/messages/",
                        headers=client._auth(), params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    raise

        msgs = data.get("messages", []) if isinstance(data, dict) else data
        if not msgs:
            break

        all_msgs.extend(msgs)
        if len(msgs) < 100:
            break

        offset_id = msgs[-1].get("id")
        await asyncio.sleep(0.2)

    return all_msgs


# ─── Фильтры ──────────────────────────────────────────────────────────────────

SKIP_PHRASES = [
    "Пришлите, пожалуйста, Ваш номер телефона",
    "Пришлём презентацию и условия",
    "[Системное сообщение]",
]

def _is_skip(text: str) -> bool:
    return any(p in text for p in SKIP_PHRASES)


# ─── Основная логика ──────────────────────────────────────────────────────────

async def main():
    client_id     = os.getenv("AVITO_CLIENT_ID", "")
    client_secret = os.getenv("AVITO_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("❌ Задай AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в .env")
        return

    client = AvitoClient(client_id=client_id, client_secret=client_secret, name="Экспорт")

    print(f"\n🔑 Подключаемся к Авито...")
    uid_self = await client.get_user_id()
    print(f"✅ user_id = {uid_self}")

    print(f"\n📋 Загружаем чаты (макс. {CHAT_LIMIT})...")
    all_chats = await get_all_chats(client)
    total = len(all_chats)
    print(f"\n{'─'*40}")
    print(f"  Чатов для обработки: {total}")
    print(f"{'─'*40}")

    # buyer_id → {"name": str, "messages": [(ts, role, text)], "seen": set}
    by_buyer: dict[str, dict] = {}
    errors = 0

    print(f"\n💬 Читаем диалоги...\n")

    for i, chat in enumerate(all_chats, 1):
        chat_id  = chat.get("id", "")
        last_msg = (chat.get("last_message") or {})
        last_ts  = last_msg.get("created", 0)
        last_date = datetime.fromtimestamp(last_ts).strftime("%d.%m.%Y") if last_ts else "?"
        last_text = (last_msg.get("content") or {}).get("text", "")[:35]

        pct = int(i / total * 100)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"[{bar}] {pct:3d}%  {i}/{total}  {last_date}  {last_text!r}",
              end="\r", flush=True)

        try:
            # Детали чата
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=60) as http:
                        r = await http.get(
                            f"{BASE_URL}/messenger/v2/accounts/{uid_self}/chats/{chat_id}",
                            headers=client._auth(),
                        )
                        r.raise_for_status()
                        chat_info = r.json()
                    break
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(3)
                    else:
                        raise

            messages = await get_all_messages(client, chat_id)

            users      = chat_info.get("users", [])
            buyer      = next((u for u in users if u.get("id") != uid_self), {})
            buyer_id   = str(buyer.get("id", ""))
            buyer_name = buyer.get("name", "Клиент")
            self_name  = next(
                (u.get("name", "Муза") for u in users if u.get("id") == uid_self), "Муза"
            )

            messages.sort(key=lambda m: m.get("created", 0))

            seen_msgs: set = by_buyer[buyer_id]["seen"] if buyer_id in by_buyer else set()
            new_msgs = []

            for msg in messages:
                if msg.get("type") != "text":
                    continue
                text = (msg.get("content") or {}).get("text", "").strip()
                if not text or _is_skip(text):
                    continue

                ts_val = msg.get("created", 0)
                dedup_key = (ts_val, text)
                if dedup_key in seen_msgs:
                    continue

                is_out = msg.get("direction") == "out"
                role   = self_name if is_out else buyer_name
                new_msgs.append((ts_val, role, text))
                seen_msgs.add(dedup_key)

            if not new_msgs:
                await asyncio.sleep(0.3)
                continue

            if buyer_id not in by_buyer:
                by_buyer[buyer_id] = {
                    "name":     buyer_name,
                    "messages": new_msgs,
                    "seen":     seen_msgs,
                }
            else:
                by_buyer[buyer_id]["messages"].extend(new_msgs)
                by_buyer[buyer_id]["seen"] = seen_msgs

        except Exception as e:
            errors += 1
            print(f"\n  ❌ Ошибка чат {chat_id[:16]}: {type(e).__name__}: {e!r}")

        await asyncio.sleep(0.3)

    print(f"\n")

    # ─── Собираем строки ──────────────────────────────────────────────────────

    sheet_rows: list[list]  = []
    client_ids: list[str]   = []   # buyer_id для каждой строки (для окраски)

    for bid, d in by_buyer.items():
        sorted_msgs = sorted(d["messages"], key=lambda x: x[0])
        for ts_val, role, text in sorted_msgs:
            dt = datetime.fromtimestamp(ts_val)
            sheet_rows.append([
                bid,
                d["name"],
                dt.strftime("%d.%m.%Y"),
                dt.strftime("%H:%M"),
                role,
                text,
            ])
            client_ids.append(bid)

    print(f"  Итого строк для записи: {len(sheet_rows)}")
    print(f"  Уникальных клиентов:    {len(by_buyer)}")

    # ─── Пишем в Google Таблицу ───────────────────────────────────────────────

    print(f"\n📊 Подключаемся к Google Таблице...")
    ws = _get_sheet()
    print(f"  Лист «{ws.title}» открыт")

    _write_to_sheet(ws, sheet_rows, client_ids)

    ss_url = f"https://docs.google.com/spreadsheets/d/{os.getenv('SPREADSHEET_ID')}"
    print(f"\n{'─'*50}")
    print(f"✅ Готово!")
    print(f"   Клиентов:  {len(by_buyer)}")
    print(f"   Сообщений: {len(sheet_rows)}")
    if errors:
        print(f"   Ошибок:    {errors}")
    print(f"{'─'*50}")
    print(f"\nОткрой таблицу: {ss_url}\n")


if __name__ == "__main__":
    asyncio.run(main())
