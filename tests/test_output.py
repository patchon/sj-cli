from sj_cli.output import (
    _reverse_route,
    confirm,
    day_header,
    format_class_name,
    format_duration,
    group_route,
    indented,
    leg_lines,
    pad,
    pinfo,
    print_bookings_table,
    print_day_note,
    spinner,
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
