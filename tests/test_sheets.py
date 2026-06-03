"""
tests/test_sheets.py — тесты для sheets.py через unittest.mock.

gspread не вызывается — все обращения к API замоканы.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime
from unittest.mock import MagicMock, patch, call

import pytest

# ─── Заглушки для gspread и google.oauth2 до импорта sheets ──────────────────

def _make_mock_modules():
    """Регистрируем фейковые модули чтобы sheets.py импортировался без реальных зависимостей."""
    gspread_mod = types.ModuleType("gspread")
    gspread_mod.Client = MagicMock
    gspread_mod.Worksheet = MagicMock
    gspread_mod.Spreadsheet = MagicMock
    gspread_mod.authorize = MagicMock(return_value=MagicMock())
    sys.modules.setdefault("gspread", gspread_mod)

    google_mod = types.ModuleType("google")
    oauth2_mod = types.ModuleType("google.oauth2")
    sa_mod = types.ModuleType("google.oauth2.service_account")
    sa_mod.Credentials = MagicMock()
    google_mod.oauth2 = oauth2_mod
    oauth2_mod.service_account = sa_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.oauth2", oauth2_mod)
    sys.modules.setdefault("google.oauth2.service_account", sa_mod)

    # database импортируется без внешних зависимостей (только stdlib + utils),
    # поэтому не стабаем — пусть грузится настоящий модуль.


_make_mock_modules()

import sheets  # noqa: E402  (после регистрации заглушек)


# ─── Вспомогательные функции ─────────────────────────────────────────────────

DATE_FMT = "%d.%m.%Y"

def _make_ws(rows: list[list]) -> MagicMock:
    """Создаёт mock-worksheet с заданными строками."""
    ws = MagicMock()
    ws.title = "TestSheet"
    ws.get_all_values.return_value = rows
    return ws


def _patch_spreadsheet(ws_bookings=None, ws_free=None, ws_leads=None):
    """Патчим функции _sheet_* в sheets, возвращая нужные воркшиты."""
    patches = []
    if ws_bookings is not None:
        patches.append(patch.object(sheets, "_sheet_bookings", return_value=ws_bookings))
    if ws_free is not None:
        patches.append(patch.object(sheets, "_sheet_free", return_value=ws_free))
    if ws_leads is not None:
        patches.append(patch.object(sheets, "_sheet_leads", return_value=ws_leads))
    return patches


def _clear_cache():
    sheets._caches.clear()


# ─── get_free_dates ───────────────────────────────────────────────────────────

class TestGetFreeDates:
    def setup_method(self):
        _clear_cache()

    def test_returns_future_dates(self):
        today = date.today()
        future = date(today.year + 1, 6, 15)
        future_str = future.strftime(DATE_FMT)
        rows = [
            ["Дата", "День недели"],
            [future_str, "Воскресенье"],
        ]
        ws = _make_ws(rows)
        with patch.object(sheets, "_sheet_free", return_value=ws):
            result = sheets.get_free_dates(limit=10)
        assert future_str in result

    def test_skips_past_dates(self):
        rows = [
            ["Дата", "День недели"],
            ["01.01.2000", "Суббота"],
        ]
        ws = _make_ws(rows)
        with patch.object(sheets, "_sheet_free", return_value=ws):
            result = sheets.get_free_dates()
        assert result == []

    def test_respects_limit(self):
        today = date.today()
        future_rows = [
            [date(today.year + 1, m, 1).strftime(DATE_FMT), "Понедельник"]
            for m in range(1, 7)
        ]
        rows = [["Дата", "День недели"]] + future_rows
        ws = _make_ws(rows)
        with patch.object(sheets, "_sheet_free", return_value=ws):
            result = sheets.get_free_dates(limit=3)
        assert len(result) == 3

    def test_sorts_chronologically(self):
        today = date.today()
        d1 = date(today.year + 1, 3, 1).strftime(DATE_FMT)
        d2 = date(today.year + 1, 1, 1).strftime(DATE_FMT)
        rows = [["Дата", "День недели"], [d1, "Пт"], [d2, "Вс"]]
        ws = _make_ws(rows)
        with patch.object(sheets, "_sheet_free", return_value=ws):
            result = sheets.get_free_dates()
        assert result.index(d2) < result.index(d1)


# ─── check_date ───────────────────────────────────────────────────────────────

class TestCheckDate:
    def setup_method(self):
        _clear_cache()

    def test_found(self):
        target = date(2030, 8, 15)
        target_str = target.strftime(DATE_FMT)
        rows = [
            sheets.HEADERS_BOOKINGS,
            [target_str, "20", "Иван", "+79001234567",
             "ВКонтакте", "Прямой", "Без комментариев", "Пятница",
             "admin", "01.06.2026 12:00", "@ivan"],
        ]
        ws = _make_ws(rows)
        with patch.object(sheets, "_sheet_bookings", return_value=ws):
            result = sheets.check_date(target)
        assert result["found"] is True
        assert result["name"] == "Иван"
        assert result["guests"] == "20"
        assert result["tg_nick"] == "@ivan"

    def test_not_found(self):
        ws = _make_ws([sheets.HEADERS_BOOKINGS])
        with patch.object(sheets, "_sheet_bookings", return_value=ws):
            result = sheets.check_date(date(2030, 1, 1))
        assert result["found"] is False
        assert result["row"] is None


# ─── add_booking ──────────────────────────────────────────────────────────────

class TestAddBooking:
    def setup_method(self):
        _clear_cache()

    def test_adds_new_row(self):
        ws_b = _make_ws([sheets.HEADERS_BOOKINGS])
        ws_f = _make_ws([sheets.HEADERS_FREE])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            sheets.add_booking(
                date(2030, 9, 1), guests="30", name="Анна",
                phone="+79991112233", source="Инстаграм",
                client_type="Агентство", comment="",
                changed_by="admin", tg_nick="@anna",
            )
        ws_b.update.assert_called_once()
        uploaded = ws_b.update.call_args[0][1]
        # первая строка — заголовок, вторая — данные
        assert uploaded[1][2] == "Анна"
        assert uploaded[1][10] == "@anna"

    def test_overwrites_existing_date(self):
        target = date(2030, 9, 1)
        target_str = target.strftime(DATE_FMT)
        existing_row = [target_str, "10", "Старый", "+70000000000",
                        "", "", "", "Понедельник", "", "", ""]
        ws_b = _make_ws([sheets.HEADERS_BOOKINGS, existing_row])
        ws_f = _make_ws([sheets.HEADERS_FREE])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            sheets.add_booking(
                target, guests="50", name="Новый",
                phone="+79991112233", source="", client_type="", comment="",
            )
        ws_b.update.assert_called_once()
        uploaded = ws_b.update.call_args[0][1]
        # строк должно быть ровно 2 (заголовок + 1 запись)
        data_rows = [r for r in uploaded if any(r)]
        assert len(data_rows) == 2
        names = [r[2] for r in data_rows if r[2]]
        assert "Новый" in names
        assert "Старый" not in names

    def test_removes_from_free(self):
        target = date(2030, 9, 1)
        target_str = target.strftime(DATE_FMT)
        ws_b = _make_ws([sheets.HEADERS_BOOKINGS])
        ws_f = _make_ws([sheets.HEADERS_FREE, [target_str, "Понедельник"]])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            sheets.add_booking(
                target, guests="10", name="X", phone="", source="", client_type="", comment=""
            )
        # ws_f.update должен был вызваться для удаления даты из «Свободные»
        ws_f.update.assert_called()


# ─── remove_booking ───────────────────────────────────────────────────────────

class TestRemoveBooking:
    def setup_method(self):
        _clear_cache()

    def test_removes_and_returns_true(self):
        target = date(2030, 10, 10)
        target_str = target.strftime(DATE_FMT)
        ws_b = _make_ws([
            sheets.HEADERS_BOOKINGS,
            [target_str, "5", "Кто-то", "", "", "", "", "Пятница", "", "", ""],
        ])
        ws_f = _make_ws([sheets.HEADERS_FREE])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            result = sheets.remove_booking(target)
        assert result is True
        ws_b.update.assert_called_once()

    def test_returns_false_if_not_found(self):
        ws_b = _make_ws([sheets.HEADERS_BOOKINGS])
        ws_f = _make_ws([sheets.HEADERS_FREE])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            result = sheets.remove_booking(date(2030, 10, 10))
        assert result is False

    def test_adds_back_to_free(self):
        target = date(2030, 10, 10)
        target_str = target.strftime(DATE_FMT)
        ws_b = _make_ws([
            sheets.HEADERS_BOOKINGS,
            [target_str, "5", "Тест", "", "", "", "", "Пятница", "", "", ""],
        ])
        ws_f = _make_ws([sheets.HEADERS_FREE])
        with patch.object(sheets, "_sheet_bookings", return_value=ws_b), \
             patch.object(sheets, "_sheet_free", return_value=ws_f):
            sheets.remove_booking(target)
        ws_f.update.assert_called()


# ─── edit_booking ─────────────────────────────────────────────────────────────

class TestEditBooking:
    def setup_method(self):
        _clear_cache()

    def test_edits_name(self):
        target = date(2030, 11, 5)
        target_str = target.strftime(DATE_FMT)
        ws = _make_ws([
            sheets.HEADERS_BOOKINGS,
            [target_str, "10", "Старое имя", "+7999", "", "", "", "Среда", "", "", ""],
        ])
        with patch.object(sheets, "_sheet_bookings", return_value=ws):
            result = sheets.edit_booking(target, changed_by="admin", name="Новое имя")
        assert result is True
        uploaded = ws.update.call_args[0][1]
        names = [r[2] for r in uploaded if len(r) > 2 and r[2]]
        assert "Новое имя" in names

    def test_returns_false_if_not_found(self):
        ws = _make_ws([sheets.HEADERS_BOOKINGS])
        with patch.object(sheets, "_sheet_bookings", return_value=ws):
            result = sheets.edit_booking(date(2030, 11, 5), name="X")
        assert result is False


# ─── add_lead ─────────────────────────────────────────────────────────────────

class TestAddLead:
    def setup_method(self):
        _clear_cache()

    def test_adds_new_lead(self):
        ws = _make_ws([sheets.HEADERS_LEADS])
        with patch.object(sheets, "_sheet_leads", return_value=ws):
            sheets.add_lead("Пётр", phone="+79005556677", nick="@petr")
        ws.append_row.assert_called_once()
        args = ws.append_row.call_args[0][0]
        assert args[1] == "Пётр"
        assert args[2] == "+79005556677"

    def test_deduplicates_by_phone(self):
        existing = [sheets.HEADERS_LEADS,
                    ["01.01.2026 10:00", "Пётр", "+79005556677", "@petr", "Авито"]]
        ws = _make_ws(existing)
        with patch.object(sheets, "_sheet_leads", return_value=ws):
            sheets.add_lead("Пётр", phone="+7 900 555-66-77")
        ws.append_row.assert_not_called()

    def test_adds_nick_to_existing_lead_without_nick(self):
        existing = [sheets.HEADERS_LEADS,
                    ["01.01.2026 10:00", "Пётр", "+79005556677", "", "Авито"]]
        ws = _make_ws(existing)
        with patch.object(sheets, "_sheet_leads", return_value=ws):
            sheets.add_lead("Пётр", phone="+79005556677", nick="@petr_new")
        ws.update_cell.assert_called_once_with(2, 4, "@petr_new")

    def test_no_phone_always_appends(self):
        ws = _make_ws([sheets.HEADERS_LEADS])
        with patch.object(sheets, "_sheet_leads", return_value=ws):
            sheets.add_lead("Аноним", phone="", nick="@anon1")
            sheets.add_lead("Аноним2", phone="", nick="@anon2")
        assert ws.append_row.call_count == 2
