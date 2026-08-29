# sj-cli

*Den här filen på [svenska](README.md).*

Command-line tool that books SJ (Swedish Railways) commuter trips on a travel pass — e.g. an
*SJ Årskort* — the way the sj.se app does, but for many days at once: a date range, single days
or whole ISO weeks. It talks to the same API as the web app (reverse-engineered), so it can
authenticate, search, pick the right departure, find the 0-price pass-holder offer and check out,
day after day, skipping weekends and Swedish red days and never double-booking a day that is
already covered.

Nothing real happens with `--dry-run` present: it previews `--book`, `--book-journey`, both
cancel flags, both change-seat flags and `--upgrade-class`. A mode flag is always required —
running the tool bare just prints the help.

```
$ sj-cli --book
╭──────────────────────────────────╮
│  operation    booking tickets    │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       First Last         │
╰──────────────────────────────────╯

  route     Göteborg Central ⇄ Stockholm Central
  days      1 sep – 30 oct 2026 · weekdays only
  times     out 06:59 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

tue 15 sep 2026   Göteborg Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  ✓ checking offers for outbound at 06:59
  ✓ creating booking with outbound at 06:59
  ✓ searching return at 17:22
  ✓ checking offers for return at 17:22
  ✓ adding return leg at 17:22
  ✓ checking out booking ERU0HWB2
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 class calm   FULLFLEX   ERU0HWB2
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 66   2 class calm   FULLFLEX   ERU0HWB2

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
- `httpx` (the only runtime dependency; `pytest`, `ruff` and `mypy` for development)
- An SJ account with a travel pass, and a phone for the one-time SMS verification

## Setup

```bash
git clone https://github.com/patchon/sj-cli.git && cd sj-cli
python3 -m venv venv
./venv/bin/pip install -e .                 # runtime only; add `--group dev` for the dev tools

mkdir -p ~/.config/sj-cli
cp src/sj_cli/config.example.toml ~/.config/sj-cli/config.toml
$EDITOR ~/.config/sj-cli/config.toml      # credentials, route, dates, times
```

Or skip the copy: run `--login` in a terminal and the tool offers to create the config for you —
it asks for your SJ credentials (password never echoed), writes the documented template with
them filled in (file mode 0600), and logs you in right away. Only booking and date-based
cancelling need `[search_parameters]` (route, dates, times) edited first.

The config lives **outside** the repo on purpose — it contains your SJ password. `config.toml` in
the repo root is gitignored; only the template `src/sj_cli/config.example.toml` is tracked.

The install puts an `sj-cli` command inside the venv. Activate the venv once per shell and call it
by name, or skip activation and use the full path — the two are equivalent:

```bash
source venv/bin/activate   # leave it again with `deactivate`
sj-cli --login-status

./venv/bin/sj-cli --login-status   # same thing, no activation needed
```

### Configuration

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"     # dates and/or ISO weeks: "W36, W38..40" (this ISO year), "2027-W02..03"; past days are skipped (a selection entirely in the past is an error)
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
seat_preference = ["window", "table", "forward"]  # optional; or "ask" to be prompted
```

`seat_preference` is a ranked wish list: the best free seat wins, and an earlier word
outweighs every later one combined — `["window", "table"]` takes a plain window seat
over an aisle seat at a table. Words: `window`, `aisle`, `table`, `solo` (SJ's
first-class "Singelplats" product), `single` (a seat with no neighbour, computed from
the carriage's 2+1 layout — not the same as `solo`), `easy access`, `no animals`,
`forward`, `backward`. Set it to `"ask"` to be prompted for every leg instead (needs a
terminal), or omit the key and SJ assigns the seat. A preference is never a guarantee:
when nothing matches, the best remaining seat is taken anyway and a `!` line names the
wish it missed. The seat SJ picked is kept only when no seat is free to move to.

Every characteristic comes from SJ's seat map, and SJ warns that the map may not match the
train that actually arrives: refurbished and older X 2000 units are both in service, so three
seats per row in 2 class calm can become four, a window seat can become an aisle seat, a table
can disappear and the direction can flip. `sj-cli` reports what the map says — the train that
rolls in has the last word.

`--list-bookings --seat-details` reads the same vocabulary the other way round: it shows what
was assigned instead of choosing it, e.g. `carriage 3 seat 34 · single, window, table, forward`.
It costs one extra request per not-yet-departed leg, so it is opt-in; a leg whose seat map cannot
be read just keeps its plain `carriage N seat M` cell.

When `seat_preference` is a ranked word list, `--seat-details` also names a strictly better free
seat, when one exists, right after the assigned one: `carriage 3 seat 19 · aisle, backward ·
could take 47 · single, window, forward` — an easy way to see which tickets are worth re-seating
with `--change-seat-date`/`--change-seat-booking`. The hint needs a wish list to judge "better"
against, so it stays silent under `seat_preference = "ask"` and when the key is absent — there is
no basis for a comparison either way.

Every field is validated before any network call, and all problems are reported at once. Valid
stations are the ones in `STATION_MAP` in `src/sj_cli/client.py` (Stockholm, Linköping, Göteborg, Malmö,
Uppsala, Lund — "Central"/"C" spellings, case-insensitive). A config still using the old
`date_start`/`date_end` keys gets a one-line migration hint.

## Usage

```bash
source venv/bin/activate

sj-cli --book --dry-run        # preview: show what would be booked, without booking
sj-cli --book                  # book for real
sj-cli --book-journey          # book one journey interactively: date, from, to, return? — then pick the train from a list
sj-cli --book-journey --dry-run   # the same questions and lists, nothing booked
sj-cli --list-bookings         # active bookings as one card per travel day
sj-cli --list-bookings --seat-details   # same, plus each leg's seat (window/aisle/table/solo/
                                #   single/forward/backward) — one extra request per not-yet-departed leg;
                                #   also names a better free seat when seat_preference is a wish list
sj-cli --list-travelpasses     # passes with validity, days left and price
sj-cli --cancel-date 2026-09-16                # cancel that day's journeys on the configured route (other days of a booking are kept)
sj-cli --cancel-date 2026-09-16,2026-09-21..2026-09-25   # several dates: comma list and/or inclusive ranges
sj-cli --cancel-date W43                       # a whole ISO week (same grammar as dates)
sj-cli --cancel-booking JS3TWMF1 --dry-run     # preview a cancel: cards + what would be cancelled, no prompts
sj-cli --cancel-booking JS3TWMF1,ABCD1234    # cancel by booking number (any case)
sj-cli --change-seat-date 2026-09-28            # re-seat that day (configured route)
sj-cli --change-seat-booking ZSVV7EML           # re-seat one booking, any route
sj-cli --change-seat-date W40 --dry-run         # preview which seats it would take
sj-cli --upgrade-class W40 --dry-run            # report which booked legs could move up to comfort_class
sj-cli --upgrade-class 2026-09-28               # release and re-book those legs (asks once, terminal only)
sj-cli --login                 # authenticate, cache the token, exit
sj-cli --logout                # end the sj.se session, delete cached token + cookies
sj-cli --login-status          # exit 0 if logged in — valid or refreshable token (scripting)

LOG_LEVEL=DEBUG sj-cli --book --dry-run   # diagnostics on stderr (TRACE adds httpx wire logs)
NO_COLOR=1 sj-cli --book --dry-run        # plain output (also automatic when piped)
```

Flags are mutually exclusive and one mode flag is required (a bare run prints the help and
exits 1). Exit code 0 on success, 1 on any failure, 130 on Ctrl-C.

`--upgrade-class` is the one flag that can leave you worse off than before, so it says so up front.
SJ has no change-class operation, and the travel pass cannot hold two overlapping tickets — a
search made *with* the pass reports every class unavailable on a departure you already hold — so an
upgrade is a cancel followed by a purchase, in that order, with no way to keep the old ticket as a
safety net. Each leg is first probed with a search *without* the pass, the only search that tells
the truth about a departure you hold: it proves SJ *sells* a seat in `comfort_class`, and a leg
with no seats is never touched. It can never prove the pass will get one for free — pass quota is a
separate pool — so nothing is ever promised. If the offer is gone once the ticket is released, the
run falls down the ordinary class chain; if that fails too, that leg ends **with no ticket**,
reported per leg with the exact command that gets it back (`sj-cli --book` when the date is in your
`dates` selection, sj.se by hand otherwise), listed again before the closing status, and exit 1.
Passing the flag is the consent for that risk: it asks once, listing every leg it will attempt, and
refuses to run at all when stdin is not a terminal, so cron can never release a ticket. Only the
one journey being upgraded is cancelled — a round trip booked as one booking keeps its other leg —
and the re-booking always takes the same departure, never the one closest to `time_leave`.
`--dry-run` does the probing and reports what it would attempt, touching nothing.

`--book-journey` books a single journey the way sj.se's front page does: it asks for the date,
from, to and whether you want a return (and when), searches, then lists the outbound and the
return departures for you to pick from with the arrow keys — showing the class the pass would
get on that departure, or `no seats` where there is none. Everything you don't type comes from
config or the obvious default (today — or the pass start if the pass has not begun —
`station_from`/`station_to`, `roundtrip`, the departure closest to `time_leave`/`time_return`
highlighted), so Enter all the way through books one day of the commute; the stations can be
any in SJ's list and filter as you type. The station list shows train stations first (bus stops
only when no station matches), departures that have already left are not shown, and a departure
overlapping a ticket you already hold says which one. After a summary it asks `book?` once,
then books exactly as `--book` does (same seat choice, same checkout). If you already hold a
ticket that day it says so before the list, since a search made with the pass hides the seats
on departures overlapping it. There is no duplicate check, though — that warning is the whole
guard, and a day `--book` has already covered can be booked again. Needs a terminal;
`--dry-run` walks through every question and list without booking.

### First login

The first run performs the sj.se B2C login and asks for an SMS code (2-minute timeout). Tokens
are cached in `~/.cache/sj-cli/token.json` and refreshed automatically; SSO cookies are
cached next to it so later full logins usually skip the SMS step. `--logout` ends the sj.se
session and deletes both caches — the next login then needs the SMS step again.

## How a day is booked

For each selected date (`dates`) from today on:

1. Skip weekends / red days (configurable) — no API call.
2. Duplicate check against your existing bookings: skip the day if both legs (or the single leg)
   are already booked; otherwise search only the missing direction.
3. Round trip → one roundtrip search → one booking: outbound offer creates the provisional booking,
   the return offer is added to it (one booking number, like the SJ app).
4. For each leg: pick the departure closest to the configured time, resolve the comfort class
   (with fallback), find the 0-price pass-holder offer. If the closest departure has no such
   offer, try one alternative — earlier for the outbound, later for the return.
5. Check out. The day card ends with the booked legs exactly as `--list-bookings` will show them
   (or the reason nothing was booked). Provisional bookings left behind by an interrupted run —
   on your route, older than ten minutes — are cancelled automatically at the start of the next
   `--book` run; a cart you have open on sj.se is left alone.

Retries: transient failures on reads are retried (1 s / 2 s / 4 s); booking/checkout requests are
retried only when the request never reached the server, so a gateway hiccup can't create a
duplicate booking. Timeouts are generous (30 s) because the booking calls themselves take seconds. Full details, edge cases and message catalogue: [`SPEC.md`](SPEC.md).

## Development

```bash
./venv/bin/pip install -e . --group dev   # once: project + pytest, ruff, mypy
./venv/bin/pytest                         # ~320 tests, <1 s, no network (scripted fake client)
./venv/bin/ruff check . && ./venv/bin/ruff format --check .   # lint + formatting
./venv/bin/mypy                           # type check
```

Everything is configured in `pyproject.toml` (ruff selects ALL with documented ignores). The
editable install puts the `sj-cli` console script on the venv's path; `python -m sj_cli`
is equivalent.

Layout: standard src layout — the package is `src/sj_cli/`, one module per concern (`cli`
entry point, `auth`, `client` HTTP only, `booking` business logic, `config`, `tokens`, `logger`,
`output`, `dates`, `errors`) — see the architecture table in [`CLAUDE.md`](CLAUDE.md).
`tests/test_booking_flow.py` pins the booking flow's API call sequence and return contract; run it
after touching `booking.py`. Secrets (password, tokens, auth codes) are redacted from logs at every
level.

## Disclaimer

Unofficial, and not affiliated with or endorsed by SJ. It drives sj.se's internal web API — the
one the web app uses — which can change without notice, and automating it may not be something
SJ's terms of use allow: running this is your decision and your risk, including any consequence
for your account. Use it for your own account and pass only; you are responsible for whatever it
books.

## Licence

[GNU AGPL v3 or later](LICENSE). You may use, study, change and share it; if you distribute a
modified version — or run one as a network service that other people use — you must release your
changes under the same licence. It comes with no warranty.
