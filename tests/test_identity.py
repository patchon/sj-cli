"""The Swedish personal identity number grammar behind [compensation] and the prompt."""

from datetime import date

import pytest

from sj_cli.identity import normalise_personal_number


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("19850315-0008", "19850315-0008"),  # the twelve-digit form the site sends
        ("198503150008", "19850315-0008"),
        ("850315-0008", "19850315-0008"),  # ten digits: the most recent such birthday
        ("8503150008", "19850315-0008"),
        (" 19850315 0008 ", "19850315-0008"),  # whitespace and a space separator
        ("121212-1212", "20121212-1212"),  # a birthday this century when that is in the past
        ("121212+1212", "19121212-1212"),  # + marks a person past a hundred
        ("19850375-0005", "19850375-0005"),  # a coordination number: day + 60
    ],
)
def test_accepted_forms_normalise_to_the_dashed_twelve_digit_form(text, expected):
    assert normalise_personal_number(text, today=date(2026, 9, 14)) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("abc", "10 or 12 digits"),
        ("19850315000", "10 or 12 digits"),  # eleven digits
        ("19851315-0008", "not a date"),  # month 13
        ("19850315-0009", "check digit"),  # Luhn fails
        ("20300315-0004", "in the future"),
    ],
)
def test_rejected_forms_say_why(text, reason):
    with pytest.raises(ValueError, match=reason):
        normalise_personal_number(text, today=date(2026, 9, 14))


def test_masked_form_keeps_the_birth_date_only():
    from sj_cli.identity import mask_personal_number

    assert mask_personal_number("19850315-0008") == "19850315-****"
