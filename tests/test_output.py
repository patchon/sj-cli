from sj_output import (
    _group_route,
    _reverse_route,
    format_class_name,
    format_duration,
    pad,
    print_bookings_table,
    print_dry_run_table,
    style,
    visible_len,
)


def test_format_duration():
    assert format_duration("PT4H37M") == "4h 37m"
    assert format_duration("PT45M") == "45m"
    assert format_duration("PT2H") == "2h"
    assert format_duration("") == "—"
    assert format_duration("weird") == "weird"


def test_format_class_name():
    assert format_class_name("2 klass Lugn, Kan återbetalas") == "2 klass Lugn"
    assert format_class_name("") == "—"


def test_visible_len_and_pad_ignore_ansi():
    s = "\x1b[1mbold\x1b[0m"
    assert visible_len(s) == 4
    assert pad(s, 6) == s + "  "


def test_style_is_plain_without_tty():
    assert style("x", "1") == "x"


def test_routes():
    assert _reverse_route("A → B") == "B → A"
    assert _reverse_route("nonsense") == "nonsense"
    assert _group_route([{"route": "A → B"}, {"route": "B → A"}]) == "A ⇄ B"
    assert _group_route([{"route": "A → B"}, {"route": "A → C"}]) == "A → B · A → C"


def test_dry_run_table_shows_note_in_flexibility_cell(capsys):
    print_dry_run_table([
        {"date": "2026-09-01", "direction": "Outbound", "departure": "06:59", "arrival": "11:36",
         "comfort_class": "2 class calm", "flexibility": "FULLFLEX", "note": ""},
        {"date": "2026-09-01", "direction": "Return", "departure": "17:22", "arrival": "19:01",
         "comfort_class": "2 class", "flexibility": "—", "note": "no 0-price offer"},
    ])
    out = capsys.readouterr().out
    assert "06:59      11:36    2 class calm  FULLFLEX" in out
    assert "17:22      19:01    2 class       no 0-price offer" in out
    assert "n/a" not in out


def test_bookings_card_groups_by_day_and_infers_return_arrow(capsys):
    legs = [
        {"date": "2026-09-01", "departure": "06:59", "arrival": "11:36", "duration": "4h 37m",
         "route": "A → B", "booking_number": "NUM1", "past": "N", "train": "X 2000 520",
         "seat": "carriage 3 seat 1", "comfort_class": "2 klass"},
        {"date": "2026-09-01", "departure": "17:22", "arrival": "21:53", "duration": "4h 31m",
         "route": "B → A", "booking_number": "NUM1", "past": "N", "train": "X 2000 543",
         "seat": "carriage 3 seat 2", "comfort_class": "2 klass"},
    ]
    print_bookings_table(legs, "Pass")
    out = capsys.readouterr().out
    assert "tue 01 sep 2026   A ⇄ B" in out
    assert "→ 06:59 – 11:36" in out
    assert "← 17:22 – 21:53" in out
    assert "1 day(s) · 1 booking(s) · 2 leg(s)" in out
