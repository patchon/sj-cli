# sj-api-client

Command-line tool that books SJ (Swedish Railways) commuter trips on a travel pass — e.g. an
*SJ Årskort* — the way the sj.se app does, but for a whole date range at once. It talks to the same
API as the web app (reverse-engineered), so it can authenticate, search, pick the right departure,
find the 0-price pass-holder offer and check out, day after day, skipping weekends and Swedish red
days and never double-booking a day that is already covered.

Nothing real happens with `--dry-run` present: it previews `--book` and both cancel flags.
A mode flag is always required — running the tool bare just prints the help.

```
$ python3 sj_tool.py --book
╭──────────────────────────────────╮
│  operation    booking tickets    │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       First Last         │
╰──────────────────────────────────╯

  route     Linköping Central ⇄ Stockholm Central
  days      1 sep – 30 oct 2026 · weekdays only
  times     out 06:59 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

tue 15 sep 2026   Linköping Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  ✓ checking offers for outbound at 06:59
  ✓ creating booking with outbound at 06:59
  ✓ searching return at 17:22
  ✓ checking offers for return at 17:22
  ✓ adding return leg at 17:22
  ✓ checking out booking ERU0HWB2
  → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 17   2 klass Lugn   ERU0HWB2
  ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 66   2 klass Lugn   ERU0HWB2

wed 16 sep 2026   tickets already booked

sat 19 sep 2026   weekend

3 day(s) · 1 booked · 1 already booked · 1 skipped
```

Every pass-scoped mode (book, dry run, cancel, list) opens with a header box naming the
operation, configured account, travel pass and holder. Book and dry run follow with the run's config as labelled
facts, a dim progress trail, one card per travel day (bold date + route, legs beneath), and a
dim summary; dry run shows the legs it *would* book in the same cards. `--list-bookings` shows
the booked day cards with a `N day(s) · N booking(s)` footer, `--list-travelpasses` one card per
pass. The auth modes (`--login`, `--logout`, `--login-status`) answer with a status card instead:
a green/red dot + verdict, then labelled facts (account, session horizon, token expiry).

## Requirements

- Python 3.13+
- `httpx`, `typing_extensions` (runtime); `pytest` (tests only)
- An SJ account with a travel pass, and a phone for the one-time SMS verification

## Setup

```bash
git clone <this repo> && cd sj-api-client
python3 -m venv venv
./venv/bin/pip install httpx typing_extensions pytest

mkdir -p ~/.config/sj-api-client
cp config.example.toml ~/.config/sj-api-client/config.toml
$EDITOR ~/.config/sj-api-client/config.toml      # credentials, route, dates, times
```

The config lives **outside** the repo on purpose — it contains your SJ password. `config.toml` in
the repo root is gitignored; only `config.example.toml` is tracked.

### Configuration

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
date_start = "2026-09-01"
date_end = "2026-10-30"
time_leave = "06:59"                 # preferred outbound departure (HH:MM, Swedish time)
time_return = "17:22"                # preferred return departure; required when roundtrip = true
station_from = "Göteborg Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"       # "1 class", "2 class", "2 class calm"
flexibility = "FULLFLEX"             # FULLFLEX, SEMIFLEX, NOFLEX
roundtrip = true
select_closest_ticket_available = true   # closest departure in time; false = exact time only
allow_class_fallback = true          # optional: 2 class calm → 2 class if the class is unavailable
book_partial = false                 # optional: book the return leg alone if the outbound is unavailable
skip_weekends = true                 # optional
skip_holidays = true                 # optional: Swedish red days incl. Midsommar-/Jul-/Nyårsafton
service_types = ["SJ_HIGH", "SJ_IC"] # optional train-type filter; omit or ["ALL"] for none
```

Every field is validated before any network call, and all problems are reported at once. Valid
stations are the ones in `STATION_MAP` in `sj_client.py` (Stockholm, Linköping, Göteborg, Malmö,
Uppsala, Lund — "Central"/"C" spellings, case-insensitive).

## Usage

```bash
source venv/bin/activate

python3 sj_tool.py --book --dry-run        # preview: show what would be booked, without booking
python3 sj_tool.py --book                  # book for real
python3 sj_tool.py --list-bookings         # active bookings as one card per travel day
python3 sj_tool.py --list-travelpasses     # passes with validity, days left and price
python3 sj_tool.py --cancel-date 2026-09-16                # cancel that day's bookings on the configured route
python3 sj_tool.py --cancel-date 2026-09-16,2026-09-21..2026-09-25   # several dates: comma list and/or inclusive ranges
python3 sj_tool.py --cancel-booking JS3TWMF1 --dry-run     # preview a cancel: cards + what would be cancelled, no prompts
python3 sj_tool.py --cancel-booking JS3TWMF1,ABCD1234    # cancel by booking number (any case)
python3 sj_tool.py --login                 # authenticate, cache the token, exit
python3 sj_tool.py --logout                # end the sj.se session, delete cached token + cookies
python3 sj_tool.py --login-status          # exit 0 if logged in — valid or refreshable token (scripting)

LOG_LEVEL=DEBUG python3 sj_tool.py --book --dry-run   # diagnostics on stderr (TRACE adds httpx wire logs)
NO_COLOR=1 python3 sj_tool.py --book --dry-run        # plain output (also automatic when piped)
```

Flags are mutually exclusive and one mode flag is required (a bare run prints the help and
exits 1). Exit code 0 on success, 1 on any failure, 130 on Ctrl-C.

### First login

The first run performs the sj.se B2C login and asks for an SMS code (2-minute timeout). Tokens
are cached in `~/.cache/sj-api-client/token.json` and refreshed automatically; SSO cookies are
cached next to it so later full logins usually skip the SMS step. `--logout` ends the sj.se
session and deletes both caches — the next login then needs the SMS step again.

## How a day is booked

For each date in `[date_start, date_end]`:

1. Skip weekends / red days (configurable) — no API call.
2. Duplicate check against your existing bookings: skip the day if both legs (or the single leg)
   are already booked; otherwise search only the missing direction.
3. Round trip → one roundtrip search → one booking: outbound offer creates the provisional booking,
   the return offer is added to it (one booking number, like the SJ app).
4. For each leg: pick the departure closest to the configured time, resolve the comfort class
   (with fallback), find the 0-price pass-holder offer. If the closest departure has no such
   offer, try one alternative — earlier for the outbound, later for the return.
5. Check out. The day card ends with the booked legs exactly as `--list-bookings` will show them
   (or the reason nothing was booked). Provisional bookings left behind by an interrupted run are
   cancelled automatically at the start of the next `--book` run.

Retries: transient failures on reads are retried (1 s / 2 s / 4 s); booking/checkout requests are
retried only when the request never reached the server, so a gateway hiccup can't create a
duplicate booking. Full details, edge cases and message catalogue: [`SPEC.md`](SPEC.md).

## Development

```bash
./venv/bin/pytest      # ~140 tests, <1 s, no network (scripted fake client)
ruff check .           # lint; ruff.toml selects ALL with documented ignores
```

Layout: one module per concern (`sj_tool` entry point, `sj_auth`, `sj_client` HTTP only,
`sj_booking` business logic, `sj_config`, `sj_token`, `sj_logger`, `sj_output`, `sj_calendar`,
`sj_errors`) — see the architecture table in [`CLAUDE.md`](CLAUDE.md). `tests/test_booking_flow.py`
pins the booking flow's API call sequence and return contract; run it after touching
`sj_booking.py`. Secrets (password, tokens, auth codes) are redacted from logs at every level.

## Disclaimer

Unofficial. It uses sj.se's internal web API, which can change without notice. Use it for your
own account and pass only; you are responsible for whatever it books.
