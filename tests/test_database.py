"""
tests/test_database.py — юнит-тесты для database.py

Запуск:
    cd muza_bot && python -m pytest tests/ -v

Все тесты работают с временной БД в памяти — реальная muza.db не затрагивается.
"""

import os
import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import database


# ─── Фикстура: временная БД ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Перенаправляем DB_PATH во временный файл на время каждого теста."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", db_file)
    database.init_db()
    yield db_file


# ─── init_db ─────────────────────────────────────────────────────────────────

def test_init_db_creates_tables(tmp_db):
    con = sqlite3.connect(tmp_db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    assert {"bookings", "free_dates", "leads", "allowed_users", "access_log",
            "seen_users", "avito_chat_state"} <= tables


def test_init_db_idempotent(tmp_db):
    """Повторный вызов init_db не ломает схему."""
    database.init_db()
    database.init_db()
    con = sqlite3.connect(tmp_db)
    count = con.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    con.close()
    assert count == 0


# ─── upsert_booking / check_date / delete_booking ────────────────────────────

def test_upsert_and_check_date():
    target = date(2026, 8, 15)
    database.upsert_booking(target, guests="50", name="Иван", phone="+79001234567",
                            source="Авито", client_type="Прямой", comment="тест")
    result = database.check_date(target)
    assert result["found"] is True
    assert result["name"] == "Иван"
    assert result["guests"] == "50"


def test_check_date_not_found():
    result = database.check_date(date(2030, 1, 1))
    assert result["found"] is False


def test_upsert_booking_updates_existing():
    target = date(2026, 9, 10)
    database.upsert_booking(target, name="Анна", guests="30")
    database.upsert_booking(target, name="Мария", guests="40")
    result = database.check_date(target)
    assert result["name"] == "Мария"
    assert result["guests"] == "40"


def test_delete_booking_returns_true():
    target = date(2026, 10, 5)
    database.upsert_booking(target, name="Пётр")
    assert database.delete_booking(target) is True
    assert database.check_date(target)["found"] is False


def test_delete_booking_missing_returns_false():
    assert database.delete_booking(date(2030, 12, 31)) is False


def test_get_all_bookings_sorted_by_date_iso():
    d1 = date(2026, 12, 1)
    d2 = date(2026, 6, 1)
    d3 = date(2027, 1, 1)
    for d in [d3, d1, d2]:
        database.upsert_booking(d, name="test")
    bookings = database.get_all_bookings()
    dates = [b["date_obj"] for b in bookings]
    assert dates == sorted(dates)


# ─── free_dates ───────────────────────────────────────────────────────────────

def test_add_and_get_free_dates():
    today = date.today()
    future = today + timedelta(days=5)
    database.add_free_date(future, "Сб")
    free = database.get_free_dates(limit=10)
    assert future.strftime("%d.%m.%Y") in free


def test_get_free_dates_excludes_past():
    past = date.today() - timedelta(days=1)
    database.add_free_date(past, "Пн")
    free = database.get_free_dates(limit=100)
    assert past.strftime("%d.%m.%Y") not in free


def test_remove_free_date():
    future = date.today() + timedelta(days=10)
    database.add_free_date(future, "Вт")
    database.remove_free_date(future)
    free = database.get_free_dates(limit=100)
    assert future.strftime("%d.%m.%Y") not in free


# ─── seen_users ───────────────────────────────────────────────────────────────

def test_has_seen_user_false_initially():
    assert database.has_seen_user(99999) is False


def test_mark_user_seen():
    database.mark_user_seen(12345)
    assert database.has_seen_user(12345) is True


def test_mark_user_seen_idempotent():
    database.mark_user_seen(55555)
    database.mark_user_seen(55555)  # второй вызов не должен падать
    assert database.has_seen_user(55555) is True


# ─── avito_chat_state ─────────────────────────────────────────────────────────

def test_upsert_avito_chat_state_and_load():
    database.upsert_avito_chat_state(
        "chat_abc",
        greeted=True,
        last_handled_id="msg_001",
        ctx_date="2026-08-15",
        faq_count=2,
    )
    rows = database.get_all_avito_chat_states()
    row = next((r for r in rows if r["chat_id"] == "chat_abc"), None)
    assert row is not None
    assert row["greeted"] == 1
    assert row["last_handled_id"] == "msg_001"
    assert row["ctx_date"] == "2026-08-15"
    assert row["faq_count"] == 2


def test_upsert_avito_chat_state_updates():
    database.upsert_avito_chat_state("chat_xyz", greeted=True, faq_count=1)
    database.upsert_avito_chat_state("chat_xyz", greeted=True, faq_count=5, ignored=True)
    rows = database.get_all_avito_chat_states()
    row = next(r for r in rows if r["chat_id"] == "chat_xyz")
    assert row["faq_count"] == 5
    assert row["ignored"] == 1


def test_get_all_avito_chat_states_multiple():
    for i in range(3):
        database.upsert_avito_chat_state(f"chat_{i}", lead_received=True)
    rows = database.get_all_avito_chat_states()
    ids = {r["chat_id"] for r in rows}
    assert {"chat_0", "chat_1", "chat_2"} <= ids


# ─── allowed_users ────────────────────────────────────────────────────────────

def test_add_and_check_allowed_user():
    database.add_allowed_user(111111, "@testadmin")
    assert database.is_allowed_user(111111) is True


def test_is_allowed_user_unknown():
    assert database.is_allowed_user(999999) is False


def test_remove_allowed_user():
    database.add_allowed_user(222222, "@todel")
    users = database.get_allowed_users()
    row_id = next(u["id"] for u in users if u["username"] == "todel")
    assert database.remove_allowed_user(row_id) is True
    assert database.is_allowed_user(222222) is False
