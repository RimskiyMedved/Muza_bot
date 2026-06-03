"""
tests/test_utils.py — юнит-тесты для utils.py

Запуск:
    cd muza_bot && python -m pytest tests/ -v
"""

from datetime import date

import pytest

from utils import (
    extract_phone,
    extract_tg_nick,
    fmt_phone,
    has_phone,
    has_tg_nick,
    parse_date,
    parse_all_dates,
)


# ─── parse_date ───────────────────────────────────────────────────────────────

class TestParseDate:
    def test_dd_mm_yyyy(self):
        assert parse_date("20.05.2026") == date(2026, 5, 20)

    def test_d_m_yyyy(self):
        assert parse_date("5.6.2026") == date(2026, 6, 5)

    def test_day_month_name_ru(self):
        # Используем будущий месяц (август), чтобы год не сдвигался
        result = parse_date("20 августа")
        assert result is not None
        assert result.month == 8 and result.day == 20

    def test_day_month_name_genitive(self):
        result = parse_date("15 сентября")
        assert result is not None
        assert result.month == 9 and result.day == 15

    def test_day_month_name_with_year(self):
        assert parse_date("10 апреля 2027") == date(2027, 4, 10)

    def test_iso_format(self):
        assert parse_date("2026-06-15") == date(2026, 6, 15)

    def test_garbage_returns_none(self):
        assert parse_date("привет мир") is None

    def test_empty_returns_none(self):
        assert parse_date("") is None

    def test_just_number_returns_none(self):
        assert parse_date("42") is None


class TestParseAllDates:
    def test_single_date(self):
        # Август — гарантированно в будущем (тест запускается в июне 2026)
        result = parse_all_dates("Хочу забронировать 15 августа")
        assert len(result) == 1
        assert result[0].month == 8 and result[0].day == 15

    def test_two_dates(self):
        result = parse_all_dates("Могу 10 июня или 17 июня")
        assert len(result) == 2

    def test_no_dates(self):
        assert parse_all_dates("Добрый день") == []


# ─── has_phone / extract_phone ────────────────────────────────────────────────

class TestPhone:
    def test_has_phone_11_digits(self):
        assert has_phone("89001234567")

    def test_has_phone_plus7(self):
        assert has_phone("+79001234567")

    def test_has_phone_formatted(self):
        assert has_phone("8 (900) 123-45-67")

    def test_no_phone_in_text(self):
        assert not has_phone("Позвоните мне как-нибудь")

    def test_extract_russian_mobile(self):
        phone = extract_phone("Мой номер +7 900 123-45-67")
        assert phone is not None
        assert "9001234567" in phone.replace(" ", "").replace("-", "").replace("+7", "7")

    def test_extract_phone_not_found(self):
        assert extract_phone("нет номера здесь") == ""

    def test_fmt_phone_11_digits(self):
        # fmt_phone нормализует к "+7XXXXXXXXXX" (без пробелов)
        assert fmt_phone("89001234567") == "+79001234567"

    def test_fmt_phone_short_passthrough(self):
        # Короткий ввод — возвращаем как есть
        result = fmt_phone("123")
        assert result  # не падает


# ─── has_tg_nick / extract_tg_nick ───────────────────────────────────────────

class TestTgNick:
    def test_has_nick_at_prefix(self):
        assert has_tg_nick("Пишите мне @muza_zal")

    def test_no_nick(self):
        assert not has_tg_nick("просто текст без ника")

    def test_extract_nick(self):
        assert extract_tg_nick("Мой ник @test_user123") == "@test_user123"

    def test_extract_nick_not_found(self):
        assert extract_tg_nick("никаких собак") == ""
