import datetime as dt

from azmonitor.parsers.ssc_headline import normalise_label, period_from_header, period_from_label
from azmonitor.util.numbers import parse_number
from azmonitor.util.periods import month_end, prev_month_end, shift_months


def test_azerbaijani_number_formats():
    assert parse_number("87 709,5").value == 87709.5
    assert parse_number("+1,2%").value == 1.2 and parse_number("+1,2%").is_percent
    assert parse_number("-0,8%").value == -0.8
    assert parse_number("1 176,7").value == 1176.7
    assert parse_number(1922.2).value == 1922.2
    assert parse_number("15422.9").value == 15422.9


def test_missing_value_markers_are_not_zero():
    for marker, reason in [("x", "not_comparable"), ("…", "not_available"), ("-", "dash"), ("", "blank"), (None, "blank"), ("#REF!", "spreadsheet_error")]:
        p = parse_number(marker)
        assert p.value is None and p.missing_reason == reason


def test_multiple_marker_converted_to_growth():
    p = parse_number("+2,8 d.")
    assert abs(p.value - 180.0) < 1e-9 and "multiple_converted_to_percent_growth" in p.flags


def test_published_zero_marker_flagged():
    p = parse_number("0,0")
    assert p.value == 0.0 and "published_zero_marker" in p.flags


def test_footnote_digits_stripped_from_labels():
    assert normalise_label("Orta aylıq nominal əməkhaqqı 2 , manat (2026-cı ilin yanvar-iyul ayları üzrə)").startswith("Orta aylıq nominal əməkhaqqı, manat")
    assert normalise_label("Ticarət dövriyyəsinin həcmi2") == "Ticarət dövriyyəsinin həcmi"
    assert normalise_label("İqtisadiyyata kredit qoyuluşları1, milyon manat").startswith("İqtisadiyyata kredit qoyuluşları, milyon")


def test_mixed_cutoffs_in_one_table():
    # stock "as at 01.08.2026" -> end-July; shorter YTD window in the label; page headline Jan-Aug
    assert period_from_label("İqtisadiyyata kredit qoyuluşları, milyon manat (01.08.2026-cı il vəziyyətinə)") == ("stock", dt.date(2026, 7, 31))
    assert period_from_label("Orta aylıq nominal əməkhaqqı, manat (2026-cı ilin yanvar-iyul ayları üzrə)") == ("ytd", dt.date(2026, 7, 31))
    assert period_from_header("Göstəricinin adı 2026-cı ilin yanvar - avqust ayları") == dt.date(2026, 8, 31)
    assert period_from_header("2025-ci il, faktiki") == dt.date(2025, 12, 31)


def test_month_end_and_shift():
    assert month_end(2026, 2) == dt.date(2026, 2, 28)
    assert month_end(2024, 2) == dt.date(2024, 2, 29)
    assert prev_month_end(dt.date(2026, 8, 1)) == dt.date(2026, 7, 31)
    assert shift_months(dt.date(2026, 1, 31), -1) == dt.date(2025, 12, 31)
    assert shift_months(dt.date(2026, 3, 31), -12) == dt.date(2025, 3, 31)
