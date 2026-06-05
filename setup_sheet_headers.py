"""
setup_sheet_headers.py — одноразовый скрипт.

Обновляет первую строку листа «Бронирования» до нового формата
с финансовыми колонками (L–T).

Запуск:
    python3 setup_sheet_headers.py
"""

from sheets import _sheet_bookings, _sheet_free, _sheet_leads, HEADERS_BOOKINGS, HEADERS_FREE, HEADERS_LEADS

def update_headers(ws, headers: list[str], sheet_label: str):
    current = ws.row_values(1)
    if current == headers:
        print(f"✅ {sheet_label}: заголовки уже актуальны ({len(headers)} столбцов)")
        return
    # Обновляем только первую строку
    ws.update([headers], "A1")
    print(f"✅ {sheet_label}: заголовки обновлены ({len(current)} → {len(headers)} столбцов)")
    if len(current) < len(headers):
        new_cols = headers[len(current):]
        print(f"   Новые столбцы: {new_cols}")

if __name__ == "__main__":
    print("Подключаемся к Google Sheets...")
    update_headers(_sheet_bookings(), HEADERS_BOOKINGS, "Бронирования")
    update_headers(_sheet_free(),     HEADERS_FREE,     "Свободные")
    update_headers(_sheet_leads(),    HEADERS_LEADS,    "Авито")
    print("\nГотово. Существующие данные не тронуты.")
