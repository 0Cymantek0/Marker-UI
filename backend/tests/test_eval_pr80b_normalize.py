"""Task-normalization unit tests for the PR80B benchmark (matrix N)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.eval.pr80b.normalize import (
    normalize_by_type,
    normalize_currency,
    normalize_date,
    normalize_decimal,
    normalize_integer,
    normalize_string,
)


class TestDateNormalization:
    def test_iso_passthrough(self):
        assert normalize_date("2026-03-01").value == "2026-03-01"

    def test_us_slash_normalized_us_first(self):
        assert normalize_date("03/15/2026").value == "2026-03-15"
        assert normalize_date("12/31/2026").value == "2026-12-31"

    def test_us_slash_unpadded(self):
        assert normalize_date("4/2/2026").value == "2026-04-02"

    def test_month_name_long_form(self):
        assert normalize_date("April 2, 2026").value == "2026-04-02"
        assert normalize_date("March 4, 2026").value == "2026-03-04"

    def test_invalid_month_name(self):
        assert normalize_date("Frimble 2, 2026").error is not None

    def test_iso_with_bad_month_fails(self):
        assert normalize_date("2026-13-01").error is not None

    def test_day_out_of_range_fails(self):
        assert normalize_date("02/30/2026").error is not None

    def test_ambiguous_european_slash_is_rejected_not_guessed(self):
        # 31 cannot be a US month; the declared convention is US-first,
        # so this must fail rather than silently flipping to D/M.
        assert normalize_date("31/12/2026").error is not None

    def test_empty_fails(self):
        assert normalize_date("").error is not None
        assert normalize_date(None).error is not None


class TestDecimalNormalization:
    def test_plain_decimal_keeps_numeric_equality(self):
        assert normalize_decimal("155.00").value == Decimal("155")
        assert normalize_decimal("155.0").value == normalize_decimal("155.00").value

    def test_us_thousands_stripped(self):
        assert normalize_decimal("3,750.00").value == Decimal("3750")
        assert normalize_decimal("1,540.00").value == Decimal("1540")

    def test_eu_dot_thousands_comma_decimal(self):
        assert normalize_decimal("2.045,00").value == Decimal("2045")
        assert normalize_decimal("1.540,00").value == Decimal("1540")

    def test_eu_plain_comma_decimal(self):
        assert normalize_decimal("195,00").value == Decimal("195")
        assert normalize_decimal("11,50").value == Decimal("11.5")

    def test_us_thousands_wins_over_three_digit_comma_group(self):
        # "1,234" is US thousands (1234), not EU decimal 1.23 with a stray digit.
        assert normalize_decimal("1,234").value == Decimal("1234")

    def test_negative_decimal(self):
        assert normalize_decimal("-45.00").value == Decimal("-45")
        assert normalize_decimal("-1.540,00").value == Decimal("-1540")

    def test_non_numeric_fails(self):
        assert normalize_decimal("N/A").error is not None
        assert normalize_decimal("abc").error is not None

    def test_empty_fails(self):
        assert normalize_decimal("").error is not None

    def test_whitespace_and_nbsp_tolerated(self):
        assert normalize_decimal(" 155.00 ").value == Decimal("155")
        assert normalize_decimal("155,00\u00a0").value == Decimal("155")


class TestIntegerNormalization:
    def test_plain_and_negative(self):
        assert normalize_integer("4").value == 4
        assert normalize_integer("-1").value == -1

    def test_us_thousands(self):
        assert normalize_integer("1,000").value == 1000

    def test_eu_comma_not_an_integer(self):
        # "1,00" is not a US thousands group and integers have no EU form.
        assert normalize_integer("1,00").error is not None

    def test_decimal_not_an_integer(self):
        assert normalize_integer("4.0").error is not None

    def test_garbage_fails(self):
        assert normalize_integer("four").error is not None


class TestCurrencyNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("USD", "USD"),
            ("usd", "USD"),
            ("$", "USD"),
            ("US$", "USD"),
            ("US Dollars", "USD"),
            ("EUR", "EUR"),
            ("€", "EUR"),
            ("Euros", "EUR"),
            ("GBP", "GBP"),
            ("£", "GBP"),
            ("Pounds Sterling", "GBP"),
        ],
    )
    def test_mapping(self, raw, expected):
        assert normalize_currency(raw).value == expected

    def test_unknown_fails(self):
        assert normalize_currency("CHF").error is not None

    def test_empty_fails(self):
        assert normalize_currency("").error is not None


class TestStringNormalization:
    def test_strips_whitespace(self):
        assert normalize_string("  INV-1 ").value == "INV-1"

    def test_empty_fails(self):
        assert normalize_string("").error is not None

    def test_none_fails(self):
        assert normalize_string(None).error is not None


class TestDispatch:
    def test_enum_exact_token_wins(self):
        assert normalize_by_type("enum", "USD", ("USD", "EUR", "GBP")).value == "USD"

    def test_enum_falls_back_to_currency_map(self):
        assert normalize_by_type("enum", "$", ("USD", "EUR", "GBP")).value == "USD"

    def test_unsupported_type_fails(self):
        assert normalize_by_type("float", "1.5").error is not None
