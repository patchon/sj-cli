from sj_output import (
    _group_route,
    _reverse_route,
    day_header,
    format_class_name,
    format_duration,
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
    assert _group_route([{"route": "A → B"}, {"route": "B → A"}]) == "A ⇄ B"
    assert _group_route([{"route": "A → B"}, {"route": "A → C"}]) == "A → B · A → C"


def test_leg_lines_omit_empty_columns_and_put_note_in_flexibility_cell():
    rows = [
        {"departure": "06:59", "arrival": "11:36", "duration": "4h 37m", "train": "X 2000 520",
         "route": "A → B", "comfort_class": "2 class calm", "flexibility": "FULLFLEX", "note": ""},
        {"departure": "17:22", "arrival": "21:53", "duration": "4h 31m", "train": "X 2000 543",
         "route": "B → A", "comfort_class": "2 class", "flexibility": "", "note": "no 0-price offer"},
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
    rows = [{"departure": "06:59", "arrival": "11:36", "route": "A → B", "booking_number": "NUM1", "past": "Y"}]
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
        {"date": "2026-09-01", "departure": "06:59", "arrival": "11:36", "duration": "4h 37m",
         "route": "A → B", "booking_number": "NUM1", "past": "N", "train": "X 2000 520",
         "seat": "carriage 3 seat 1", "comfort_class": "2 klass"},
        {"date": "2026-09-01", "departure": "17:22", "arrival": "21:53", "duration": "4h 31m",
         "route": "B → A", "booking_number": "NUM1", "past": "N", "train": "X 2000 543",
         "seat": "carriage 3 seat 2", "comfort_class": "2 klass"},
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
    from sj_output import print_travelpasses

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
    from sj_output import print_travelpasses

    print_travelpasses([{"name": "SJ Årskort", "code": "123"}], None)
    out = capsys.readouterr().out
    assert "SJ Årskort   123" in out
    assert "holder" not in out
    assert "price" not in out


def test_status_card_with_plain_lines(capsys):
    from sj_output import print_status_card

    print_status_card(False, "invalid configuration",
                      lines=["time_leave must be a valid time (HH:MM, 24-hour)"])
    out = capsys.readouterr().out
    assert "● invalid configuration" in out
    assert "\n   time_leave must be a valid time (HH:MM, 24-hour)\n" in out


def test_ask_inline_prompt_reads_and_closes_line(monkeypatch, capsys):
    from sj_output import ask

    monkeypatch.setattr("builtins.input", lambda: "a")
    assert ask("select [1/2/a]: ") == "a"
    # '?' marker, inline prompt; newline added because stdin isn't a tty
    assert capsys.readouterr().out == " ? select [1/2/a]: \n"


def test_ask_returns_empty_on_eof(monkeypatch, capsys):
    from sj_output import ask

    def _raise_eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    assert ask("cancel? [y/n]: ") == ""
    assert capsys.readouterr().out == " ? cancel? [y/n]: \n"


def test_header_box(capsys):
    from sj_output import print_header_box

    print_header_box([
        ("operation", "dry run · booking tickets"),
        ("travelpass", "SJ Årskort Silver"),
        ("holder", "John Doe"),
    ])
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

    import sj_output

    monkeypatch.setattr(sj_output, "color_enabled", lambda: True)
    with sj_output.spinner("step"):
        pass
    out = capsys.readouterr().out
    assert "\x1b[32m✓\x1b[0m \x1b[2mstep\x1b[0m" in out  # green mark, dim text
    with pytest.raises(ValueError, match="boom"), sj_output.spinner("bad"):
        raise ValueError("boom")
    assert "\x1b[31m✗\x1b[0m \x1b[2mbad\x1b[0m" in capsys.readouterr().out


def test_pwarn_yellow_mark_dim_text(monkeypatch, capsys):
    import sj_output

    sj_output.pwarn("outbound class fallback: 1 class → 2 class calm")
    assert capsys.readouterr().out == " ! outbound class fallback: 1 class → 2 class calm\n"
    monkeypatch.setattr(sj_output, "color_enabled", lambda: True)
    sj_output.pwarn("careful")
    assert capsys.readouterr().out == " \x1b[33m!\x1b[0m \x1b[2mcareful\x1b[0m\n"


def test_pstatus_dot_by_outcome(monkeypatch, capsys):
    import sj_output

    sj_output.pstatus(True, "22 day(s) · 34 booking(s)")
    sj_output.pstatus(False, "cancellation aborted")
    assert capsys.readouterr().out == " ● 22 day(s) · 34 booking(s)\n ● cancellation aborted\n"
    monkeypatch.setattr(sj_output, "color_enabled", lambda: True)
    sj_output.pstatus(True, "done")
    sj_output.pstatus(False, "failed")
    out = capsys.readouterr().out
    assert "\x1b[32m●\x1b[0m \x1b[2mdone\x1b[0m" in out
    assert "\x1b[31m●\x1b[0m \x1b[2mfailed\x1b[0m" in out
