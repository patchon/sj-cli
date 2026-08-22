from datetime import date

from sj_api_client.dates import easter_sunday, skip_reason, swedish_holidays


def test_easter_known_years():
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)


def test_holidays_2026():
    h = swedish_holidays(2026)
    assert h[date(2026, 4, 3)] == "Långfredagen"
    assert h[date(2026, 4, 6)] == "Annandag påsk"
    assert h[date(2026, 5, 14)] == "Kristi himmelsfärdsdag"
    assert h[date(2026, 6, 19)] == "Midsommarafton"
    assert h[date(2026, 6, 20)] == "Midsommardagen"
    assert h[date(2026, 10, 31)] == "Alla helgons dag"
    assert h[date(2026, 12, 24)] == "Julafton"
    assert len(h) == 16


def test_skip_reason():
    assert skip_reason(date(2026, 9, 5), True, True) == "weekend"  # Saturday
    assert skip_reason(date(2026, 9, 5), False, True) is None
    assert skip_reason(date(2026, 12, 25), True, True) == "Juldagen"
    assert skip_reason(date(2026, 12, 25), True, False) is None
    assert skip_reason(date(2026, 9, 1), True, True) is None
