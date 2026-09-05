# sj-cli

*Den här filen på [svenska](README.md).*

Command-line tool that books SJ (Swedish Railways) trips for you on a travel
pass, e.g. an *SJ Årskort* (annual pass) or *SJ 30-dagarskort* (30-day pass).

This tool is entirely *vibe-coded*: all of the code was written by
[Claude](https://claude.ai), from my instructions. I directed, reviewed and
tested; I did not write the code.

![sj-cli demo](demo.gif)

## Why

An SJ travel pass does not let you board any train you like. Every trip has to
be booked, just like an ordinary ticket. The difference is that the booking
costs 0 kr, since the trip is already paid for through the pass.

What you may not realise when you buy the pass is that you are not guaranteed
a seat on the departures you want. If the train is sold out in your class you
simply cannot book a ticket, which in turn is rather disappointing when you
have paid so much for a monthly pass, and even more so for an annual one.

In practice this means booking your trip well in advance, which is not always
possible. Booking the day before is, on the route I commute, close to
impossible, since by then it is always "sold out".

The most frustrating part is that "sold out" does not mean full. In all the
years I have commuted I have never boarded **a single train** where every seat
in my class was taken, even though no ticket could be booked precisely because
the train was "sold out". The staff on board have also been clear that the
train is "fully booked" and that everyone therefore has to sit in their booked
seat. Despite that there have, as I said, always been free seats in my class.

It gets more frustrating still when the staff ask for my ticket and I explain
that none could be booked, whereupon they explain that it is because the train
is full. That is rather paradoxical, since I would argue that I am the actual
physical proof that this is not the case, with more empty seats around me, it
should be added.

Only SJ can say why. One guess is that pass holders book seats they then never
use; another that SJ counts a train as full with a generous margin; a third
that a seat booked for part of the route counts as taken the whole way.
Whatever the cause, it is very disappointing not to be able to take the train
you want when you have paid so much for your pass, especially when the train
is not even full (which it never is).

## The fix?

Only SJ can solve it, but a few conceivable ways would be:

* requiring a booking to be confirmed a day before departure to remain valid,
* upgrading to the next class instead of refusing when one is sold out,
* or simply a seat count that matches what the train actually looks like.

Without insight into the cause it is of course hard to know what would help.

The only thing I know is that I want to get on the train, and to do that I have
to book tickets weeks and months ahead. Ironically, that makes me part of the
problem. In this case I still choose to put my own commute first *(sorry, you
who did not get a ticket on a day I had booked but did not travel)*.

## The tool

This tool does not fix SJ's problem, but with it I can at least book every trip
I intend to make, weeks and months ahead.

It books your trips the way the sj.se app does, but for many days at once.
Single trips can also be booked interactively. You specify:

* date ranges, single days or whole ISO weeks
* departure time
* one-way or return
* comfort class
* flexibility
* seat preferences
* whether weekends and red days should be skipped

The tool then books the tickets that match your wishes, day by day; a day that
is already booked is never booked twice. It talks to the same API as the sj.se
web app (reverse-engineered, nothing official), which lets it log in, search,
pick the right departure, find the pass holder's 0 kr offer and check out.

## Requirements

- Python 3.13+
- `httpx` (the only runtime dependency; `pytest`, `ruff` and `mypy` for development)
- An SJ account with a travel pass, and a phone for the one-time SMS verification

## Installation

```bash
git clone https://github.com/patchon/sj-cli.git
cd sj-cli
python3 -m venv venv
./venv/bin/pip install -e .
source venv/bin/activate
mkdir -p ~/.config/sj-cli
cp src/sj_cli/config.example.toml ~/.config/sj-cli/config.toml
```

## Configuration

```bash
$EDITOR ~/.config/sj-cli/config.toml
```

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"
time_leave = "04:01"
time_return = "17:22"
station_from = "Malmö Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"
flexibility = "FULLFLEX"
roundtrip = true
select_closest_ticket_available = true
allow_class_fallback = true
book_partial = true
skip_weekends = true
skip_holidays = true
service_types = ["SJ_HIGH"]
seat_preference = ["avoid table", "single", "aisle", "window", "forward"]
```

With the configuration above the tool will:

* book return trips on weekdays *(weekends and red days are skipped)*
* between **2026-09-01** and **2026-10-30**
* from **Malmö Central** to **Stockholm Central**
* departing at **04:01** from Malmö and **17:22** from Stockholm
* in 2 class calm *(falling back to 2 class when calm is full)*
* book one direction only when a return trip cannot be had
* take the closest departure when the configured time cannot be booked
* on SJ high-speed trains only, with the seat chosen by the seat preference

## Examples

### Book tickets per the configuration

```bash
$ > sj-cli --book
  ╭──────────────────────────────────╮
  │  operation    booking tickets    │
  │  account      jane@doe           │
  │  travelpass   SJ Periodkort      │
  │  holder       Jane Doe           │
  ╰──────────────────────────────────╯

  route     Malmö Central ⇄ Stockholm Central
  days      15 sep – 20 sep 2026 · weekdays only
  times     out 04:01 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

  tue 15 sep 2026   Malmö Central ⇄ Stockholm Central
    ✓ searching outbound at 04:01
    ✓ checking offers for outbound at 04:01
    ✓ creating booking with outbound at 04:01
    ✓ searching return at 17:22
    ✓ checking offers for return at 17:22
    ✓ adding return leg at 17:22
    ✓ checking out booking ERU0HWB2
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 45   2 class calm   FULLFLEX   ERU0HWB2
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 11   2 class calm   FULLFLEX   ERU0HWB2

  wed 16 sep 2026   tickets already booked

  thu 17 sep 2026   tickets already booked

  fri 18 sep 2026   tickets already booked

  sat 19 sep 2026   weekend

  sun 20 sep 2026   weekend

  ● 6 day(s) · 1 booked · 3 already booked · 2 skipped
```

### List bookings

```bash
$ > sj-cli --list-bookings
  ╭──────────────────────────────────╮
  │  operation    listing bookings   │
  │  account      jane@doe           │
  │  travelpass   SJ Periodkort      │
  │  holder       Jane Doe           │
  ╰──────────────────────────────────╯

  mon 12 oct 2026   Malmö Central ⇄ Stockholm Central
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 67   2 class calm   FULLFLEX   WXYZ1234
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 11   2 class calm   FULLFLEX   WXYZ1234

  tue 13 oct 2026   Malmö Central ⇄ Stockholm Central
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 27   2 class calm   FULLFLEX   W3ST1234
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 27   2 class calm   FULLFLEX   W3ST1234

  ● 2 day(s) · 2 booking(s)
```

### Other

Use `--dry-run` to see what would happen: the flag previews `--book`,
`--book-journey`, `--cancel-date`, `--cancel-booking`, `--change-seat-date`,
`--change-seat-booking` and `--upgrade-class`.

Use `--seat-details` together with `--list-bookings` to get seat information.

You can also skip copying the config file: run `--login` in a terminal and the
tool offers to create the configuration for you, asking for your email and
password. The trip parameters must still be filled in by hand.

Supported environment variables:

```bash
LOG_LEVEL=DEBUG|TRACE # diagnostics on stderr (TRACE adds httpx wire logs)
NO_COLOR=1            # plain output (also automatic when piped)
```

See `sj-cli --help` for the full help and every command-line flag.

### Login

The tool handles sj.se's B2C login and asks for an SMS code (two-minute
timeout). The token is cached in `~/.cache/sj-cli/token.json` and refreshed
automatically; SSO cookies are cached next to it, so later full logins usually
skip the SMS step. `--logout` ends the sj.se session and deletes both caches;
the next login then needs the SMS step again.

## Development

```bash
./venv/bin/pip install -e . --group dev
./venv/bin/pytest                                             # ~589 tests, <1 s, no network (scripted fake client)
./venv/bin/ruff check . && ./venv/bin/ruff format --check .   # lint + formatting
./venv/bin/mypy                                               # type check
```

Everything is configured in `pyproject.toml` (ruff selects ALL with documented
ignores). The editable install puts the `sj-cli` console script on the venv's
path; `python -m sj_cli` is equivalent.

Layout: standard src layout — the package is `src/sj_cli/`, one module per
concern (`cli` entry point, `auth`, `client` HTTP only, `booking` business
logic, `config`, `tokens`, `logger`, `output`, `dates`, `seats`, `stations`,
`journey`, `errors`) — see the architecture table in [`CLAUDE.md`](CLAUDE.md).
`tests/test_booking_flow.py` pins the booking flow's API call sequence and
return contract; run it after touching `booking.py`. Secrets (password, tokens,
auth codes) are redacted from logs at every level.

## Disclaimer

Unofficial, and not affiliated with or endorsed by SJ. It drives sj.se's
internal web API — the one the web app uses — which can change without notice,
and automating it may not be something SJ's terms of use allow: running this is
your decision and your risk, including any consequence for your account. Use it
for your own account and pass only; you are responsible for whatever it books.

## Licence

[GNU AGPL v3 or later](LICENSE). You may use, study, change and share it; if
you distribute a modified version — or run one as a network service that other
people use — you must release your changes under the same licence. It comes
with no warranty.
