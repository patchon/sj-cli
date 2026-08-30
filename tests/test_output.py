import os
import re

import pytest

from sj_cli import output
from sj_cli.errors import SJError
from sj_cli.output import (
    _read_key,
    _reverse_route,
    confirm,
    day_header,
    departure_choice_lines,
    format_duration,
    group_route,
    indented,
    leg_lines,
    pad,
    pinfo,
    print_bookings_table,
    print_day_note,
    select_filtered,
    select_list,
    spinner,
    split_product_name,
    style,
    visible_len,
)


def test_format_duration():
    assert format_duration("PT4H37M") == "4h 37m"
    assert format_duration("PT45M") == "45m"
    assert format_duration("PT2H") == "2h"
    assert format_duration("") == "—"
    assert format_duration("weird") == "weird"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2 klass Lugn, Kan återbetalas", ("2 class calm", "FULLFLEX")),
        ("1 klass, Kan ej ombokas", ("1 class", "NOFLEX")),
        ("2 klass, Kan ombokas", ("2 class", "SEMIFLEX")),
        ("2 klass", ("2 class", None)),
        ("2 class calm, FULLFLEX", ("2 class calm", "FULLFLEX")),  # already ours: untouched
        ("Buss, Något annat", ("Buss", "Något annat")),  # unknown words pass through
        ("", ("—", None)),
        ("—", ("—", None)),
    ],
)
def test_split_product_name(raw, expected):
    assert split_product_name(raw) == expected


def test_visible_len_and_pad_ignore_ansi():
    s = "\x1b[1mbold\x1b[0m"
    assert visible_len(s) == 4
    assert pad(s, 6) == s + "  "


def test_style_is_plain_without_tty():
    assert style("x", "1") == "x"


def test_routes():
    assert _reverse_route("A → B") == "B → A"
    assert _reverse_route("nonsense") == "nonsense"
    assert group_route([{"route": "A → B"}, {"route": "B → A"}]) == "A ⇄ B"
    assert group_route([{"route": "A → B"}, {"route": "A → C"}]) == "A → B · A → C"


def test_leg_lines_omit_empty_columns_and_put_note_in_flexibility_cell():
    rows = [
        {
            "departure": "06:59",
            "arrival": "11:36",
            "duration": "4h 37m",
            "train": "X 2000 520",
            "route": "A → B",
            "comfort_class": "2 class calm",
            "flexibility": "FULLFLEX",
            "note": "",
        },
        {
            "departure": "17:22",
            "arrival": "21:53",
            "duration": "4h 31m",
            "train": "X 2000 543",
            "route": "B → A",
            "comfort_class": "2 class",
            "flexibility": "",
            "note": "no 0-price offer",
        },
    ]
    lines = leg_lines(rows)
    assert lines == [
        "→ 06:59 – 11:36   4h 37m   X 2000 520   2 class calm   FULLFLEX",
        "← 17:22 – 21:53   4h 31m   X 2000 543   2 class        no 0-price offer",
    ]
    # seat / booking number columns are absent because no row has them
    assert "—" not in "".join(lines)
    assert leg_lines([]) == []


def test_leg_lines_dim_past_and_bold_number_without_colour():
    rows = [
        {
            "departure": "06:59",
            "arrival": "11:36",
            "route": "A → B",
            "booking_number": "NUM1",
            "past": "Y",
        }
    ]
    assert leg_lines(rows) == ["→ 06:59 – 11:36   NUM1"]


def test_pinfo_keeps_case_and_indents_inside_block(capsys):
    pinfo("booking ERU0HWB2 cancelled")
    with indented():
        pinfo("inner")
        with spinner("step"):
            pass
        with spinner("quiet", trail=False):
            pass
    pinfo("outer")
    assert capsys.readouterr().out == " booking ERU0HWB2 cancelled\n   inner\n   ✓ step\n outer\n"


def test_day_header_and_note(capsys):
    assert day_header("2026-09-15", "A ⇄ B") == "tue 15 sep 2026   A ⇄ B"
    print_day_note("2026-09-19", "weekend")
    assert capsys.readouterr().out == " sat 19 sep 2026   weekend\n"


def test_bookings_card_groups_by_day_and_infers_return_arrow(capsys):
    legs = [
        {
            "date": "2026-09-01",
            "departure": "06:59",
            "arrival": "11:36",
            "duration": "4h 37m",
            "route": "A → B",
            "booking_number": "NUM1",
            "past": "N",
            "train": "X 2000 520",
            "seat": "carriage 3 seat 1",
            "comfort_class": "2 klass",
        },
        {
            "date": "2026-09-01",
            "departure": "17:22",
            "arrival": "21:53",
            "duration": "4h 31m",
            "route": "B → A",
            "booking_number": "NUM1",
            "past": "N",
            "train": "X 2000 543",
            "seat": "carriage 3 seat 2",
            "comfort_class": "2 klass",
        },
    ]
    print_bookings_table(legs)
    out = capsys.readouterr().out
    # card-first: no title, no leading blank
    assert out.startswith(" tue 01 sep 2026   A ⇄ B")
    assert "→ 06:59 – 11:36" in out
    assert "← 17:22 – 21:53" in out
    # footer: no emoji, no leg count
    assert "\n ● 1 day(s) · 1 booking(s)\n" in out
    assert "leg(s)" not in out
    assert "🚆" not in out


def test_travelpass_cards(capsys):
    from sj_cli.output import print_travelpasses

    tp = {
        "name": "SJ Årskort Silver",
        "code": "9752209715585832",
        "holder": {"firstName": "John", "lastName": "Doe", "email": "p@x.se"},
        "startTravelValidityDateTime": "2026-08-02T00:00:00+02:00",
        "endTravelValidityDateTime": "2027-08-02T00:00:00+02:00",
        "travelPassCreationBookingId": "B1",
    }
    print_travelpasses([tp], {"B1": {"amount": 51250, "currency": "SEK"}})
    out = capsys.readouterr().out
    assert not out.startswith("\n")  # card-first mode: no title above, no extra blank
    assert out.startswith(" SJ Årskort Silver   9752209715585832")
    assert "  holder    John Doe (p@x.se)" in out
    assert "  valid     2026-08-02 – 2027-08-01 (" in out  # exclusive end date
    assert "days left)" in out
    assert "  price     51250 SEK" in out
    assert "● 1 travel pass(es)" in out
    assert "─" not in out  # no table rulers anywhere


def test_travelpass_card_omits_unknown_facts(capsys):
    from sj_cli.output import print_travelpasses

    print_travelpasses([{"name": "SJ Årskort", "code": "123"}], None)
    out = capsys.readouterr().out
    assert "SJ Årskort   123" in out
    assert "holder" not in out
    assert "price" not in out


def test_status_card_with_plain_lines(capsys):
    from sj_cli.output import print_status_card

    print_status_card(
        False, "invalid configuration", lines=["time_leave must be a valid time (HH:MM, 24-hour)"]
    )
    out = capsys.readouterr().out
    assert "● invalid configuration" in out
    assert "\n   time_leave must be a valid time (HH:MM, 24-hour)\n" in out


def test_ask_inline_prompt_reads_and_closes_line(monkeypatch, capsys):
    from sj_cli.output import ask

    monkeypatch.setattr("builtins.input", lambda: "a")
    assert ask("select [1/2/a]: ") == "a"
    # '?' marker, inline prompt; newline added because stdin isn't a tty
    assert capsys.readouterr().out == " ? select [1/2/a]: \n"


def test_ask_returns_empty_on_eof(monkeypatch, capsys):
    from sj_cli.output import ask

    def _raise_eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert ask("cancel? [y/n]: ") == ""
    assert capsys.readouterr().out == " ? cancel? [y/n]: \n"


def test_header_box(capsys):
    from sj_cli.output import print_header_box

    print_header_box(
        [
            ("operation", "dry run · booking tickets"),
            ("travelpass", "SJ Årskort Silver"),
            ("holder", "John Doe"),
        ]
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(" ╭─") and lines[0].endswith("╮")
    assert lines[-1].startswith(" ╰─") and lines[-1].endswith("╯")
    assert "operation    dry run · booking tickets" in lines[1]
    assert "travelpass   SJ Årskort Silver" in lines[2]
    assert "holder       John Doe" in lines[3]
    for line in lines[1:-1]:
        assert line.startswith(" │") and line.endswith("│")
    assert len({len(line) for line in lines}) == 1  # all lines equal width


def test_trail_marks_are_coloured(monkeypatch, capsys):
    import pytest

    from sj_cli import output

    monkeypatch.setattr(output, "color_enabled", lambda: True)
    with output.spinner("step"):
        pass
    out = capsys.readouterr().out
    assert "\x1b[92m✓\x1b[0m \x1b[2mstep\x1b[0m" in out  # green mark, dim text
    with pytest.raises(ValueError, match="boom"), output.spinner("bad"):
        raise ValueError("boom")
    assert "\x1b[91m✗\x1b[0m \x1b[2mbad\x1b[0m" in capsys.readouterr().out


def test_pwarn_yellow_mark_dim_text(monkeypatch, capsys):
    from sj_cli import output

    output.pwarn("outbound class fallback: 1 class → 2 class calm")
    assert capsys.readouterr().out == " ! outbound class fallback: 1 class → 2 class calm\n"
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    output.pwarn("careful")
    assert capsys.readouterr().out == " \x1b[93m!\x1b[0m \x1b[2mcareful\x1b[0m\n"


def test_pstatus_dot_by_outcome(monkeypatch, capsys):
    from sj_cli import output

    output.pstatus(True, "22 day(s) · 34 booking(s)")
    output.pstatus(False, "cancellation aborted")
    output.pstatus(None, "1 travel pass(es)")
    assert capsys.readouterr().out == (
        " ● 22 day(s) · 34 booking(s)\n ● cancellation aborted\n ● 1 travel pass(es)\n"
    )
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    output.pstatus(True, "done")
    output.pstatus(False, "failed")
    output.pstatus(None, "nothing changed")
    out = capsys.readouterr().out
    assert "\x1b[92m●\x1b[0m \x1b[2mdone\x1b[0m" in out
    assert "\x1b[91m●\x1b[0m \x1b[2mfailed\x1b[0m" in out
    assert "\x1b[2m●\x1b[0m \x1b[2mnothing changed\x1b[0m" in out  # dim: it only reported


def test_travelpass_footer_dot_is_dim_it_only_reports(monkeypatch, capsys):
    from sj_cli import output

    monkeypatch.setattr(output, "color_enabled", lambda: True)
    output.print_travelpasses([{"name": "P", "code": "1"}])
    assert "\x1b[2m●\x1b[0m \x1b[2m1 travel pass(es)\x1b[0m" in capsys.readouterr().out


def test_travelpass_holder_without_email_has_no_empty_parentheses(capsys):
    from sj_cli.output import print_travelpasses

    print_travelpasses(
        [{"name": "P", "code": "1", "holder": {"firstName": "Anna", "lastName": "B"}}]
    )
    out = capsys.readouterr().out
    assert "  holder    Anna B\n" in out and "()" not in out


def test_leg_lines_keep_arrows_when_the_subset_starts_with_the_return():
    from sj_cli.output import leg_lines

    ret = {"departure": "17:22", "arrival": "21:53", "route": "B → A", "direction": "Return"}
    out_ = {"departure": "06:59", "arrival": "11:36", "route": "A → B", "direction": "Outbound"}
    assert [line[0] for line in leg_lines([ret, out_])] == ["←", "→"]
    assert leg_lines([ret])[0].startswith("←")


def test_zero_receipt_amount_is_a_price_not_unknown():
    from sj_cli.output import _extract_price

    assert _extract_price({"totalAmount": {"amount": 0, "currency": "SEK"}}) == "0 SEK"
    assert _extract_price({"amount": 0}) == "0 SEK"


def test_ask_optional_reports_eof_as_none(monkeypatch):
    from sj_cli import output

    monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(EOFError))
    assert output.ask_optional("seat: ") is None
    monkeypatch.setattr("builtins.input", lambda: "")
    assert output.ask_optional("seat: ") == ""


def test_seat_choices_group_by_carriage(capsys):
    from sj_cli.output import print_seat_choices

    seats = [
        {"carriage": "3", "number": "14", "codes": ["WINDOW"], "forward": True, "single": False},
        {"carriage": "3", "number": "17", "codes": ["AISLE"], "forward": False, "single": False},
        {
            "carriage": "3",
            "number": "70",
            "codes": ["TABLE", "WINDOW"],
            "forward": True,
            "single": False,
        },
    ]
    print_seat_choices(seats, {"3": "2 class calm"})
    out = capsys.readouterr().out
    assert "free in carriage 3 · 2 class calm · 3 seats" in out
    assert "14 window, forward" in out and "70 table, window, forward" in out


def test_seat_choices_counts_one_seat_in_the_singular(capsys):
    from sj_cli.output import print_seat_choices

    print_seat_choices(
        [{"carriage": "3", "number": "14", "codes": [], "forward": True, "single": False}]
    )
    out = capsys.readouterr().out
    assert "free in carriage 3 · 1 seat\n" in out  # not "1 seats", and no comfort
    assert " · 1 seats" not in out


def test_seat_choices_list_carriages_in_numeric_order(capsys):
    from sj_cli.output import print_seat_choices

    # insertion order is 10, 2, 3: the listing must not follow it, and must
    # not sort "10" before "2" either
    seats = [
        {"carriage": "10", "number": "1", "codes": [], "forward": True, "single": False},
        {"carriage": "2", "number": "1", "codes": [], "forward": True, "single": False},
        {"carriage": "3", "number": "1", "codes": [], "forward": True, "single": False},
    ]
    print_seat_choices(seats, {"2": "1 class"})
    headers = [line for line in capsys.readouterr().out.splitlines() if "free in carriage" in line]
    assert [h.split("carriage ")[1].split(" ")[0] for h in headers] == ["2", "3", "10"]
    assert "· 1 class ·" in headers[0]  # the comfort belongs to its own carriage


def test_seat_choices_never_truncate_a_seat_with_every_property(capsys):
    from sj_cli.output import print_seat_choices

    every = ["EASY_ACCESS", "WITHOUT_ANIMALS", "SOLO", "TABLE", "WINDOW"]
    seats = [
        {"carriage": "3", "number": str(70 + i), "codes": every, "forward": True, "single": False}
        for i in range(3)
    ]
    print_seat_choices(seats)
    out = capsys.readouterr().out
    for i in range(3):
        assert f"{70 + i} easy access, no animals, solo, table, window, forward" in out
    # too wide for three columns: one entry per line, and no line runs over 80
    assert all(len(line) <= 80 for line in out.splitlines())


def test_confirm_uses_question_prompt(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert confirm("cancel booking ERU0HWB2? [y/n]: ") is True
    assert capsys.readouterr().out.startswith(" ? cancel booking ERU0HWB2? [y/n]: ")
    monkeypatch.setattr("builtins.input", lambda: "nope")
    assert confirm("cancel? [y/n]: ") is False
    monkeypatch.setattr("builtins.input", lambda: "")
    assert confirm("book? [y/N]: ") is False


# --- list widget ----------------------------------------------------------------

_CTRL = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(out):
    """Captured widget output without cursor/clear sequences (colour is already off)."""
    return _CTRL.sub("", out)


def frames(out):
    """The frames a widget drew: each starts at a carriage return."""
    return [f for f in plain(out).split("\r") if f]


@pytest.fixture(autouse=True)
def _small_terminal(monkeypatch):
    # _frame_size, not shutil.get_terminal_size: output.shutil *is* the
    # global shutil, so patching there would stub it for the whole process.
    monkeypatch.setattr(output, "_frame_size", lambda: (60, 24))


STATIONS = ["Uppsala Central", "Uppsala Norra", "Umeå Central", "Uddevalla Central", "Ulricehamn"]


def starts_with(q):
    return [s for s in STATIONS if s.lower().startswith(q.lower())]


def test_select_filtered_filters_moves_and_picks(capsys):
    keys = iter(["u", "p", "down", "enter"])
    chosen = select_filtered("to", "Stockholm Central", starts_with, str, keys=keys)
    assert chosen == "Uppsala Norra"
    out = capsys.readouterr().out
    first, after_u, after_p, after_down = frames(out)[:4]
    assert first.startswith(" ? to [Stockholm Central]: \n")
    assert "type to search · Enter keeps Stockholm Central" in first
    assert "› Uppsala Central" in after_u and "Ulricehamn" in after_u
    assert "› Uppsala Central" in after_p and "Umeå" not in after_p
    assert "› Uppsala Norra" in after_down
    assert plain(out).rstrip().endswith(" ? to: Uppsala Norra")


def test_select_filtered_enter_on_empty_query_keeps_the_default(capsys):
    assert (
        select_filtered("from", "Göteborg Central", starts_with, str, keys=iter(["enter"]))
        == "Göteborg Central"
    )
    assert plain(capsys.readouterr().out).rstrip().endswith(" ? from: Göteborg Central")


def test_select_filtered_enter_without_match_or_default_does_nothing_then_esc(capsys):
    keys = iter(["x", "enter", "esc"])
    assert select_filtered("to", None, starts_with, str, keys=keys) is None
    out = plain(capsys.readouterr().out)
    assert "no match" in out
    assert out.rstrip().endswith(" ? to: x")


def test_select_filtered_backspace_refilters_and_eof_aborts(capsys):
    keys = iter(["u", "m", "backspace", "eof"])
    assert select_filtered("to", None, starts_with, str, keys=keys) is None
    f = frames(capsys.readouterr().out)
    assert "› Umeå Central" in f[2]
    assert "› Uppsala Central" in f[3] and "Umeå" in f[3]


def test_select_filtered_frames_have_a_fixed_height(capsys):
    keys = iter(["u", "down", "enter"])
    select_filtered("to", None, starts_with, str, height=2, keys=keys)
    for frame in frames(capsys.readouterr().out)[:-1]:
        assert frame.count("\n") == 3  # prompt line + height rows + footer


def test_select_filtered_scrolls_the_window_to_the_highlight(capsys):
    keys = iter(["u", "down", "down", "down", "enter"])
    select_filtered("to", None, starts_with, str, height=2, keys=keys)
    f = frames(capsys.readouterr().out)
    assert "Uppsala Central" in f[1] and "Umeå" not in f[1]  # rows 1-2
    assert "3–4 of 5" in f[4] and "› Uddevalla Central" in f[4]  # scrolled to row 4


def test_select_filtered_clips_rows_to_the_terminal_width(capsys, monkeypatch):
    monkeypatch.setattr(output, "_frame_size", lambda: (20, 24))
    select_filtered(
        "to", None, lambda _q: ["A very long station name indeed"], str, keys=iter(["a", "esc"])
    )
    f = frames(capsys.readouterr().out)[1]
    assert "A very long st…" in f and "indeed" not in f


def test_select_list_default_arrows_digits_and_reject(capsys):
    items = ["05:29 → 09:04", "06:10 → 09:58", "07:05 → 10:40"]
    reject = lambda item: "no seats at 07:05 · pick another" if item.startswith("07") else None  # noqa: E731
    keys = iter(["down", "enter"])
    assert (
        select_list("outbound", items, str, default_index=1, reject=reject, keys=keys)
        == "05:29 → 09:04"
    )
    out = capsys.readouterr().out
    f = frames(out)
    assert f[0].startswith(" ? outbound [2]: \n") and "› 06:10" in f[0]
    # down passes over the rejected row 3 and wraps to row 1, so Enter picks it
    assert "› 05:29" in f[1] and f[1].startswith(" ? outbound [1]:")
    assert "no seats at 07:05 · pick another" not in plain(out)  # never refused: never highlighted
    assert f[-1].rstrip() == " ? outbound: 05:29 → 09:04"


def test_select_list_digits_jump_and_a_bad_number_is_refused(capsys):
    items = [f"{h:02d}:00" for h in range(5, 17)]  # 12 rows
    keys = iter(["1", "2", "enter"])
    assert select_list("outbound", items, str, keys=keys) == "16:00"
    out = plain(capsys.readouterr().out)
    assert " ? outbound [12]: 12\n" in out
    assert out.rstrip().endswith(" ? outbound: 16:00")

    keys = iter(["9", "9", "enter", "esc"])
    assert select_list("outbound", items, str, keys=keys) is None
    assert "no row 99" in plain(capsys.readouterr().out)


def test_select_list_scrolls_and_reports_the_window(capsys):
    items = [f"{h:02d}:00" for h in range(5, 17)]
    keys = iter(["up", "enter"])  # wraps to the last row
    assert select_list("return", items, str, height=4, keys=keys) == "16:00"
    f = frames(capsys.readouterr().out)
    assert "1–4 of 12" in f[0]
    assert "9–12 of 12" in f[1] and "› 16:00" in f[1]


def test_select_list_empty_items_is_none_and_no_tty_raises(monkeypatch):
    assert select_list("x", [], str) is None
    monkeypatch.setattr(output.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SJError):
        select_list("x", ["a"], str)
    with pytest.raises(SJError):
        select_filtered("x", None, lambda _q: [], str)


def test_read_key_decodes_names_utf8_and_ignores_unknown_sequences():
    r, w = os.pipe()
    try:
        os.write(
            w, b"\r\n\x7f\x08\t\x04\x1b[A\x1b[B\x1b[Z\x1bOA\x1b[1;5Cq" + "ö".encode() + b"\x1b"
        )
        got = [_read_key(r) for _ in range(13)]
        assert got == [
            "enter",
            "enter",
            "backspace",
            "backspace",
            "tab",
            "eof",
            "up",
            "down",
            "shift-tab",
            "up",
            "",
            "q",
            "ö",
        ]
        assert _read_key(r) == "esc"  # a lone Esc: nothing follows within the wait
        os.close(w)
        assert _read_key(r) == "eof"
    finally:
        os.close(r)


def test_departure_choice_lines_align_columns_and_dim_the_note():
    rows = [
        {
            "departure": "05:29",
            "arrival": "09:04",
            "duration": "3h 35m",
            "train": "X 2000 420",
            "comfort_class": "2 class calm",
            "note": "",
        },
        {
            "departure": "06:10",
            "arrival": "09:58",
            "duration": "3h 48m",
            "train": "SJ 3000 2130",
            "comfort_class": "2 class",
            "note": "fallback",
        },
        {
            "departure": "07:05",
            "arrival": "10:40",
            "duration": "3h 35m",
            "train": "X 2000 424",
            "comfort_class": "—",
            "note": "no seats",
        },
    ]
    assert departure_choice_lines(rows) == [
        "05:29 → 09:04   3h 35m   X 2000 420     2 class calm",
        "06:10 → 09:58   3h 48m   SJ 3000 2130   2 class        fallback",
        "07:05 → 10:40   3h 35m   X 2000 424     —              no seats",
    ]


def test_clip_measures_visible_width_and_never_cuts_a_reset(monkeypatch):
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    styled = "abc " + style("note", output.DIM)
    assert output._clip(styled, 40) is styled  # fits: handed back untouched
    clipped = output._clip(styled, 6)
    assert "\x1b" not in clipped  # a cut row drops its styling rather than its reset
    assert clipped == "abc n…"


def test_a_picker_wipes_its_frame_when_interrupted(capsys):
    class _CtrlC:
        def __iter__(self):
            return self

        def __next__(self):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        select_filtered("to", None, starts_with, str, keys=_CtrlC())
    out = capsys.readouterr().out
    assert plain(out).rstrip().endswith(" ? to:")  # the bare prompt line is what stays
    assert out.rindex("\x1b[K") < out.index("\x1b[J")  # the wipe comes after the last frame


def test_select_filtered_clamps_its_frame_to_a_short_terminal(capsys, monkeypatch):
    monkeypatch.setattr(output, "_frame_size", lambda: (60, 5))
    select_filtered("to", "Stockholm Central", starts_with, str, height=8, keys=iter(["enter"]))
    drawn = frames(capsys.readouterr().out)[:-1]
    assert drawn
    for frame in drawn:
        assert frame.count("\n") == 4  # prompt + 3 rows + footer: 8 clamped to 3


def test_select_list_clamps_its_frame_to_a_short_terminal(capsys, monkeypatch):
    monkeypatch.setattr(output, "_frame_size", lambda: (60, 5))
    items = [f"{h:02d}:00" for h in range(5, 17)]
    assert select_list("outbound", items, str, keys=iter(["enter"])) == "05:00"
    drawn = frames(capsys.readouterr().out)[:-1]
    assert drawn
    for frame in drawn:
        assert frame.count("\n") == 4


def test_select_filtered_scrolls_back_up(capsys):
    keys = iter(["u", "down", "down", "down", "up", "up", "up", "enter"])
    assert select_filtered("to", None, starts_with, str, height=2, keys=keys) == "Uppsala Central"
    f = frames(capsys.readouterr().out)
    assert "3–4 of 5" in f[4]  # scrolled down to row 4
    assert "1–2 of 5" in f[7] and "› Uppsala Central" in f[7]  # and back to the top


def test_select_filtered_tab_and_shift_tab_move_like_arrows():
    keys = iter(["u", "tab", "enter"])
    assert select_filtered("to", None, starts_with, str, keys=keys) == "Uppsala Norra"
    keys = iter(["u", "shift-tab", "enter"])
    assert select_filtered("to", None, starts_with, str, keys=keys) == "Ulricehamn"


def test_select_list_backspace_walks_the_typed_number_back(capsys):
    items = [f"{h:02d}:00" for h in range(5, 17)]
    keys = iter(["1", "2", "backspace", "enter"])
    assert select_list("outbound", items, str, keys=keys) == "05:00"
    f = frames(capsys.readouterr().out)
    assert f[1].startswith(" ? outbound [1]: 1\n")
    assert f[2].startswith(" ? outbound [12]: 12\n")
    assert f[3].startswith(" ? outbound [1]: 1\n")


def test_select_list_out_of_range_default_falls_back_to_the_first_row():
    items = [f"{h:02d}:00" for h in range(5, 17)]
    assert select_list("outbound", items, str, default_index=99, keys=iter(["enter"])) == "05:00"


def test_select_list_skips_refused_rows_when_moving(capsys):
    items = ["05:29", "06:10", "07:05", "08:00"]
    reject = lambda item: "held" if item in ("06:10", "07:05") else None  # noqa: E731
    keys = iter(["down", "enter"])  # from row 1 over two refused rows to row 4
    assert select_list("outbound", items, str, reject=reject, keys=keys) == "08:00"
    assert "1–4 of 4" in frames(capsys.readouterr().out)[0]
    keys = iter(["up", "enter"])  # up from row 1 wraps to row 4
    assert select_list("outbound", items, str, reject=reject, keys=keys) == "08:00"
    keys = iter(["down", "down", "enter"])  # 1 → 4 → wraps to 1
    assert select_list("outbound", items, str, reject=reject, keys=keys) == "05:29"


def test_select_list_starts_on_the_first_selectable_row(capsys):
    items = ["05:29", "06:10", "07:05"]
    reject = lambda item: "held" if item == "06:10" else None  # noqa: E731
    assert (
        select_list("outbound", items, str, default_index=1, reject=reject, keys=iter(["enter"]))
        == "07:05"
    )
    f = frames(capsys.readouterr().out)
    assert f[0].startswith(" ? outbound [3]:")


def test_select_list_refuses_a_digit_that_points_at_a_refused_row(capsys):
    items = ["05:29", "06:10", "07:05"]
    complaint = "this journey is already booked in X · pick another"
    reject = lambda item: complaint if item == "06:10" else None  # noqa: E731
    keys = iter(["2", "enter"])
    assert select_list("outbound", items, str, reject=reject, keys=keys) == "05:29"
    f = frames(capsys.readouterr().out)
    assert complaint in f[1]
    assert f[1].startswith(" ? outbound [1]:")  # the highlight stayed


def test_select_list_draws_refused_rows_dim(capsys, monkeypatch):
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    items = ["05:29", "06:10"]
    reject = lambda item: "held" if item == "06:10" else None  # noqa: E731
    select_list("outbound", items, str, reject=reject, keys=iter(["esc"]))
    out = capsys.readouterr().out
    assert "\x1b[2m" in out.split("06:10")[0][-12:]  # the refused row is wrapped in DIM


def test_select_list_with_every_row_refused_opens_and_only_esc_leaves(capsys):
    items = ["05:29", "06:10"]
    reject = lambda _item: "overlaps booking H · pick another"  # noqa: E731
    keys = iter(["down", "enter", "esc"])
    assert select_list("outbound", items, str, reject=reject, keys=keys) is None
    out = plain(capsys.readouterr().out)
    assert "overlaps booking H · pick another" in out
