"""stations.py: folding, ranking and exact lookup over SJ's station list (pure)."""

from sj_cli.stations import StationIndex, fold, parse_stations

RAW = [
    {"name": "Stockholm Central", "uicStationCode": "740000001", "synonyms": ["1"]},
    {
        "name": "Göteborg Central",
        "uicStationCode": "740000002",
        "synonyms": ["2", "Gothenburg Central station"],
    },
    {"name": "Malmö Central", "uicStationCode": "740000003", "synonyms": ["3"]},
    {"name": "Uppsala Central", "uicStationCode": "740000005", "synonyms": []},
    {"name": "Uppsala Norra", "uicStationCode": "740000123", "synonyms": None},
    {"name": "Göteborg Stockholmsgatan", "uicStationCode": "740024470", "synonyms": ["24470"]},
    {"name": "Nameless", "uicStationCode": None},
    {"uicStationCode": "740099999"},
    "junk",
    # Lowest code of the lot, but only a word-prefix match for "uppsala"/"upps"
    # (rank 2) and a name-prefix match for "central" (rank 1) — pins that rank
    # beats code order, not the other way around.
    {"name": "Centralstationen Uppsala", "uicStationCode": "740000000", "synonyms": []},
]


def names(stations):
    return [s["name"] for s in stations]


def test_parse_stations_keeps_named_coded_entries_only():
    stations = parse_stations(RAW)
    assert names(stations) == [
        "Stockholm Central",
        "Göteborg Central",
        "Malmö Central",
        "Uppsala Central",
        "Uppsala Norra",
        "Göteborg Stockholmsgatan",
        "Centralstationen Uppsala",
    ]
    assert stations[1] == {
        "name": "Göteborg Central",
        "code": "740000002",
        "synonyms": ["2", "Gothenburg Central station"],
    }
    assert stations[4]["synonyms"] == []


def test_parse_stations_accepts_a_wrapped_list_and_nothing():
    assert names(parse_stations({"stations": RAW[:1]})) == ["Stockholm Central"]
    assert parse_stations(None) == []


def test_fold_strips_case_diacritics_and_spacing():
    assert fold("  Göteborg   Central ") == "goteborg central"
    assert fold("MALMÖ") == "malmo"


def test_match_ranks_exact_prefix_word_substring_then_synonym():
    idx = StationIndex(parse_stations(RAW))
    assert names(idx.match("Uppsala Central"))[0] == "Uppsala Central"
    assert names(idx.match("upps")) == [
        "Uppsala Central",
        "Uppsala Norra",
        "Centralstationen Uppsala",
    ]
    assert names(idx.match("central")) == [
        "Centralstationen Uppsala",
        "Stockholm Central",
        "Göteborg Central",
        "Malmö Central",
        "Uppsala Central",
    ]
    assert names(idx.match("holm")) == ["Stockholm Central", "Göteborg Stockholmsgatan"]
    assert names(idx.match("gothen")) == ["Göteborg Central"]
    # "2" is Göteborg Central's synonym (rank 0) and inside "24470" (rank 4)
    assert names(idx.match("2")) == ["Göteborg Central", "Göteborg Stockholmsgatan"]


def test_match_is_case_and_diacritic_insensitive():
    idx = StationIndex(parse_stations(RAW))
    assert names(idx.match("GOTEBORG")) == ["Göteborg Central", "Göteborg Stockholmsgatan"]
    assert names(idx.match("malmo")) == ["Malmö Central"]


def test_match_empty_query_is_empty():
    idx = StationIndex(parse_stations(RAW))
    assert idx.match("") == []
    assert idx.match("   ") == []


def test_exact_looks_up_names_and_synonyms():
    idx = StationIndex(parse_stations(RAW))
    assert idx.exact("göteborg central")["code"] == "740000002"
    assert idx.exact("Gothenburg Central station")["code"] == "740000002"
    assert idx.exact("Nowhere") is None
    assert idx.exact("") is None


def test_match_rank_beats_code_order():
    # "Centralstationen Uppsala" (740000000) has the lowest code of the whole
    # list, but for "uppsala" it is only a word-prefix match (rank 2) while
    # "Uppsala Central" and "Uppsala Norra" are name-prefix matches (rank 1).
    # Sorting by code alone would put it first; it must sort last instead.
    idx = StationIndex(parse_stations(RAW))
    assert names(idx.match("uppsala")) == [
        "Uppsala Central",
        "Uppsala Norra",
        "Centralstationen Uppsala",
    ]


def test_exact_prefers_the_lowest_code_on_a_tie():
    # Same name, listed higher-code first so list order can't mask the tie-break.
    stations = parse_stations(
        [
            {"name": "Alvesta", "uicStationCode": "740000900", "synonyms": []},
            {"name": "Alvesta", "uicStationCode": "740000090", "synonyms": []},
        ]
    )
    idx = StationIndex(stations)
    assert idx.exact("alvesta")["code"] == "740000090"
