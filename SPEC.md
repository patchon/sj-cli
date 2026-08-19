# SPEC.md

Specification for the SJ API Client — a Python CLI tool that reverse-engineers sj.se to automatically book daily commute train tickets using an annual travel pass (e.g., SJ Årskort).

## 1. Purpose

Book train tickets for a daily commute over a date range (typically 1–3 months) using the 0-price benefit of an SJ travel pass. The tool authenticates via SJ's Azure AD B2C flow, searches for departures matching the user's time preferences, and books the best available option for each date.

This is **not** a general-purpose booking tool. It handles one route, one passenger (the pass holder), repeated across a date range.

## 2. Architecture

Plain venv, no package manager or build system. Modules organized by separation of concerns:

| Module | Role |
|---|---|
| `sj_tool.py` | Entry point. CLI argument parsing (see §5.6), top-level orchestration. |
| `sj_auth.py` | Authentication orchestration. B2C login flow, token lifecycle (validate → refresh → full login), SMS input with timeout. |
| `sj_client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic. Includes HTTP retry logic. |
| `sj_booking.py` | Booking business logic. Search, departure selection, offer matching, provisional booking creation, checkout, cancellation, duplicate detection. |
| `sj_config.py` | Configuration loading and validation (`CfgManager`). |
| `sj_token.py` | Token cache management (`TokenManager`). Load, save, validate expiry, check refresh availability. |
| `sj_logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. |
| `sj_errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`). |
| `sj_output.py` | User-facing output helpers. `pinfo()`/`pdim()`, ANSI styling, spinner, tables for dry-run/travel passes, per-day booking cards. |
| `sj_calendar.py` | Swedish red-day calendar (Easter computed, no dependency). `skip_reason()` for weekend/holiday skipping. |

### Design principles

- **Single responsibility**: each module has one job. The HTTP client doesn't decide what to book. The booking logic doesn't manage tokens.
- **No business logic in the client**: `sj_client.py` exposes methods like `search_journey()`, `get_offers()`, `create_provisional_booking()`. It does not interpret results or make decisions.
- **Thin entry point**: `sj_tool.py` parses CLI args, wires modules together, and delegates. The main loop body calls into `sj_booking.py`.
- **Errors as exceptions**: use typed exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) rather than return-code checking. Catch at the orchestration layer.

### Data flow

```
CLI args (see §5.6)
  ↓
config.toml → sj_config.CfgManager → validate all fields
  ↓
token.json → sj_token.TokenManager → valid? → use access_token
  ↓ expired?                           ↓ no token?
sj_auth: refresh_token                sj_auth: full B2C login (interactive SMS)
  ↓ fail?                              ↓
sj_auth: full B2C login → save tokens
  ↓
Mode dispatch:
  --list-bookings         → fetch & display per-day booking cards → exit
  --list-travelpasses     → fetch & display travel-pass table → exit
  --cancel-date DATE      → find bookings on route → interactive cancel → exit
  --cancel-bookings NUMS  → find bookings by number → interactive cancel → exit
  --login-only            → exit after auth
  (default, dry run)      → search loop → display results table → exit
  --book                  → booking loop:
                              clean up stale provisionals
                              for each date in range:
                                sj_calendar: skip weekends / red days
                                sj_booking: check duplicates
                                sj_booking: search → select → offer → book → checkout
                                per-day result line
                              exit
```

## 3. Authentication

### 3.1 Token lifecycle

Follow industry-standard OAuth2 token handling:

1. **Load cached token** from `~/.cache/sj-api-client/token.json`.
2. **Validate access token**: check `expires_on` timestamp with a 5-minute safety buffer. If valid, use it.
3. **Refresh**: if access token is expired but refresh token exists, call the B2C token endpoint with `grant_type=refresh_token`. On success, cache the new token set and proceed.
4. **Full login**: if no cached token exists, or refresh fails, perform the interactive B2C login flow. This is the only path that requires user interaction.

The tool must always attempt to use what it has before falling back to a more intrusive method. Never prompt for SMS if a valid or refreshable token exists.

### 3.2 B2C login flow

Multi-step sequence against `id.sj.se` (Azure AD B2C):

1. Fetch login page → extract CSRF token and transaction ID from HTML/cookies.
2. Submit email + password credentials.
3. Confirm login stage with provider.
4. Send device fingerprint.
5. Advance to MFA orchestration step.
6. Trigger SMS MFA.
7. Prompt user for SMS code (stdin). **Timeout: 2 minutes.** If no input within 2 minutes, print an error and exit.
8. Verify SMS code.
9. Finalize login → extract authorization code from redirect.
10. Exchange authorization code for tokens (access, refresh, id token) via PKCE.

No SMS retry mechanism. If the code doesn't arrive, the user re-runs the tool.

### 3.3 Mid-run token expiry

If the access token expires during a multi-date booking loop, attempt a transparent token refresh. If refresh fails, abort with an error message explaining that re-authentication is needed. (This scenario is unlikely given typical token lifetimes vs. run duration, but the code path should exist.)

### 3.4 Credentials

Email and password stored in plaintext in `config.toml`. Acceptable for this use case.

## 4. Configuration

### 4.1 File location

`~/.config/sj-api-client/config.toml` (or `$XDG_CONFIG_HOME/sj-api-client/config.toml`)

### 4.2 Schema

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
date_start = "2026-01-19"
date_end = "2026-03-20"
time_leave = "05:29"
time_return = "17:22"
station_from = "Göteborg Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"
flexibility = "FULLFLEX"
roundtrip = true
select_closest_ticket_available = true
allow_class_fallback = true       # optional, default true
book_partial = false              # optional, default false (see §6.5)
skip_weekends = true              # optional, default true
skip_holidays = true              # optional, default true
service_types = ["SJ_HIGH", "SJ_IC"]  # optional; omit or ["ALL"] for no filter
```

### 4.3 Validation rules

All validation runs at startup before any API calls. Fail fast with clear error messages.

| Field | Rules |
|---|---|
| `email` | Required. Must match basic email format (`x@y.z`). |
| `password` | Required. Non-empty string. |
| `date_start` | Required. Valid `YYYY-MM-DD`. Must be today or in the future. |
| `date_end` | Required. Valid `YYYY-MM-DD`. Must be ≥ `date_start`. |
| `time_leave` | Required. Valid `HH:MM` (24-hour). |
| `time_return` | Required if `roundtrip = true`. Valid `HH:MM` (24-hour). |
| `station_from` | Required. Must exist in the station map. |
| `station_to` | Required. Must exist in the station map. |
| `comfort_class` | Required. One of: `"1 class"`, `"2 class"`, `"2 class calm"`. |
| `flexibility` | Required. One of: `"FULLFLEX"`, `"SEMIFLEX"`, `"NOFLEX"`. |
| `roundtrip` | Required. Boolean (`true` / `false`). |
| `select_closest_ticket_available` | Required. Boolean. |
| `allow_class_fallback` | Optional. Boolean. Defaults to `true`. |
| `book_partial` | Optional. Boolean. Defaults to `false`. See §6.5. |
| `skip_weekends` | Optional. Boolean. Defaults to `true`. Skip Saturdays and Sundays. |
| `skip_holidays` | Optional. Boolean. Defaults to `true`. Skip Swedish red days (see §6.4). |
| `service_types` | Optional. List of strings from `ALL, SJ_HIGH, SJ_IC, SJ_REG, SJ_NT, X_TRAINOPS, X_PTA, X_EXPBUS`. `ALL` cannot be combined with other values. |

Report all validation errors at once (don't stop at the first one).

### 4.4 Token cache

`~/.cache/sj-api-client/token.json` — auto-created on first successful login. Contains `access_token`, `refresh_token`, `expires_on`, etc. SSO cookies for silent re-login are cached next to it in `cookies.json`.

## 5. CLI Interface

### 5.1 Book mode

```bash
python3 sj_tool.py --book
```

Reads config, authenticates, and books tickets for every date in the configured range. No confirmation prompt — the config is the source of truth. Output is one **day card** per date (the same card shape as `--list-bookings`, §5.4): a bold date + route header, the progress trail and any messages indented beneath it, then the booked legs, and a blank line. Days that need no work are a single line (bold date + dim reason). A dim summary footer closes the run.

```
john doe · john@doe.com · sj årskort silver
✓ fetching existing bookings
29 active bookings · filter: SJ High-speed train

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

wed 16 sep 2026   already fully booked

sat 19 sep 2026   weekend

thu 24 sep 2026   Linköping Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  no departure found for outbound
  nothing booked

🚆 4 day(s) · 1 booked · 1 already booked · 1 not booked · 2 skipped
```

The booked legs are rendered from the booking object the API returns (train, carriage/seat, class, booking number), so they are identical to what `--list-bookings` shows afterwards. A checkout failure prints `checkout failed, provisional left (cleaned up on next --book run)` and counts as `checkout failed` in the footer. The route in the header shows what the day needs: `A ⇄ B` both legs, `A → B` outbound only, `B → A` return only (with a `return already booked, searching outbound only` / `outbound already booked, searching return only` line beneath).

### 5.2 Dry-run mode (default)

```bash
python3 sj_tool.py
```

Performs the full search flow for each date but does **not** create bookings. Same day cards as §5.1; each leg line shows the departure that would be booked — time range, duration, train, class, flexibility — or, dimmed in place of the flexibility cell, why it cannot be booked (`no 0-price offer`, `no departure found`). The footer starts with `🔍 dry run` and counts bookable / partly bookable / unavailable days.

```
fri 18 sep 2026   Linköping Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  ✓ checking offers for outbound at 06:59
  ✓ searching return at 17:22
  ✓ checking offers for return at 17:22
  → 04:01 – 08:38   4h 37m   X 2000 520   2 class calm   FULLFLEX
  ← 17:22 – 21:53   4h 31m   X 2000 543   2 class calm   no 0-price offer

sat 19 sep 2026   weekend

🔍 dry run · 2 day(s) · 1 partly bookable · 1 skipped
```

### 5.3 Cancel mode

```bash
python3 sj_tool.py --cancel-date 2026-01-20          # all bookings on the configured route that day
python3 sj_tool.py --cancel-bookings ERU0HWB2,8Y41N08J  # by booking number, any case
```

For each matching booking: show its day card (same shape as §5.4, no title), then confirm. `y`/`yes` (any case) confirms; anything else aborts.

```
✓ searching for booking ERU0HWB2

tue 15 sep 2026   Linköping Central → Stockholm Central
  → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 17   2 klass Lugn   ERU0HWB2

cancel booking ERU0HWB2? [y/n]: y
✓ cancelling booking ERU0HWB2
booking ERU0HWB2 cancelled
```

If the booking has several future journeys, they are listed as numbered leg lines (`1.`, `2.`, … and `a.` for all); the selected legs are echoed as leg lines under `selected for cancellation:` before the final `cancel selected journey(s)? [y/n]:`. Past journeys are shown but cannot be selected. A booking with a pending cancellation offers confirm / revert / nothing.

### 5.4 List current bookings

```bash
python3 sj_tool.py --list-bookings
```

Fetches all active bookings within the travel pass validity period and displays them as one card per travel day, legs indented beneath:

```
🎫 sj årskort silver

tue 18 aug 2026   Linköping Central ⇄ Stockholm Central   past
  → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 34   2 klass Lugn   ZR8C6RT1
  ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 22   2 klass Lugn   ZR8C6RT1

wed 19 aug 2026   Linköping Central ⇄ Stockholm Central
  → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 30   2 klass Lugn   TBRS43MG
  ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 55   2 klass Lugn   TBRS43MG

mon 31 aug 2026   Linköping Central ⇄ Stockholm Central
  → 04:01 – 08:38   4h 37m   X 2000 520   carriage 7 seat 32   2 klass        3RK7YJU4
  ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 21   2 klass Lugn   K883DH2T

🚆 3 day(s) · 4 booking(s) · 6 leg(s) · 2 in the past
```

- Header per day: date, route (`A ⇄ B` when both directions are present, otherwise the distinct routes), and a `past` tag when every leg has departed.
- Leg line: direction arrow, departure – arrival, duration, train (brand + number), carriage/seat, class, booking number. The arrow is inferred from the route (reverse of the day's first leg → `←`) because the API reports a standalone return booking as `OUTBOUND`.
- Grouped by date rather than booking number: a return leg booked via the `book_partial` fallback (§6.5) is its own booking, so the booking number is shown per leg. Legs are sorted by departure time.
- A day whose legs have all departed is dimmed; when colour is unavailable a `past` tag is appended instead.
- Only shows non-cancelled bookings. Pagination: fetches all pages from the bookings API. Read-only.
- Preamble: one dimmed context line (`name · email · travel pass`) instead of separate "logged in as"/"travel pass" lines. Progress steps show a spinner while running and leave a dimmed trail line when done — `✓ fetching bookings`, or `✗ …` if the step raised — so logs show where time went or where a step failed. When stdout is not a TTY only the trail line is printed.
- Styling: bold header/booking numbers, dimmed past days, coloured arrows. ANSI colour is emitted only when stdout is a TTY and `NO_COLOR` is unset (`TERM=dumb` also disables it); piped output is plain text. Emoji appear only in the title and summary lines, never inside aligned rows.
- The card renderer (`day_header`, `leg_lines` in `sj_output.py`) is shared by list, book, dry-run and cancel output; columns that are empty on every row of a card are omitted, so book/list cards show train · seat · class · number while dry-run cards show train · class · flexibility/note. The travel-pass table uses `format_table` (bold headers, dimmed separators).

### 5.5 Environment variables

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | Set log verbosity: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default: `CRITICAL` (silent). |
| `NO_COLOR` | If set (any value), disable ANSI colour/bold in output. Colour is also disabled automatically when stdout is not a TTY. |

### 5.6 CLI flag summary

| Flag | Description | Interactive? |
|---|---|---|
| *(none)* | Dry run: search and display what would be booked, without booking. | Only SMS on first login. |
| `--book` | Book tickets for the configured date range. | Only SMS on first login. |
| `--cancel-date YYYY-MM-DD` | Cancel bookings on the configured route for a specific date. | Yes (journey choice + confirmation). |
| `--cancel-bookings NUM[,NUM…]` | Cancel booking(s) by booking number. | Yes (journey choice + confirmation). |
| `--list-bookings` | Display all active bookings as per-day cards. | Only SMS on first login. |
| `--list-travelpasses` | Display travel passes with validity and receipt details. | Only SMS on first login. |
| `--login-only` | Authenticate, cache the token, exit. | Only SMS on first login. |
| `--test-if-already-logged-in` | Exit 0 if a valid cached token exists, else 1. No network. | No. |

Flags are mutually exclusive. Specifying more than one is a usage error (exit 1).

### 5.7 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Any failure (config error, auth error, booking error, partial failure). |

## 6. Booking Workflow

### 6.1 Per-date flow

For each date in `[date_start, date_end]`:

0. **Calendar filter**: if the date is a weekend (`skip_weekends`) or a Swedish red day (`skip_holidays`), print `skipping YYYY-MM-DD (reason)` and continue to the next date without any API calls (see §6.4).
1. **Duplicate check**: fetch existing bookings and check if a booking already exists for this (route, date). If fully booked (both legs for roundtrip, or single leg for one-way), skip with an info message.
2. **Determine what to book**: outbound only, return only, or both — based on which legs are missing.
3. **Search**: call `search_journey` with the appropriate parameters. For roundtrip where both legs are needed, use a single roundtrip search. For single missing legs, use a one-way search.
4. **Poll results**: poll `get_search_results` up to 5 times with 1-second intervals until departures appear.
5. **Select departure**: pick the best departure based on time preference and class availability (see §7).
6. **Get offers**: fetch offers for the selected departure.
7. **Find 0-price offer**: locate an offer matching the requested class + flexibility with `amount == 0`. If not found, warn and skip (see §8).
8. **Create provisional booking**: create the booking with the outbound offer.
9. **Add return leg** (if roundtrip): get offers for the return departure and add to the existing booking via PATCH.
10. **Update customer details**: set email and phone on the booking.
11. **Checkout**: finalize the booking.

### 6.2 Provisional booking cleanup

On startup (after auth, before the booking loop), fetch all existing bookings in the date range. Any booking with status `"NEW"` and `"CANCEL_JOURNEY"` in `possibleActions` is a stale provisional booking from a previous interrupted run. Cancel these automatically. This runs in `--book` mode only (dry-run must not mutate anything); the duplicate check ignores such stale provisionals in both modes so dry-run and book agree on what is already booked.

### 6.3 Timing between dates

2-second delay between processing each date to avoid hammering the API. No evidence of rate limiting at this interval over 1–3 month ranges. Skipped dates (§6.4) do not incur the delay.

### 6.4 Weekend and holiday skipping

Implemented in `sj_calendar.py` with no external dependency. `skip_reason(date, skip_weekends, skip_holidays)` returns `"weekend"`, the holiday name, or `None`.

Swedish red days are computed per year: fixed dates (Nyårsdagen, Trettondedag jul, Första maj, Nationaldagen, Juldagen, Annandag jul), Easter-derived (Långfredagen, Påskdagen, Annandag påsk, Kristi himmelsfärdsdag, Pingstdagen; Easter via the Meeus/Jones/Butcher algorithm) and window-based Saturdays (Midsommardagen = Saturday in June 20–26, Alla helgons dag = Saturday in Oct 31–Nov 6). The three eves — Midsommarafton, Julafton, Nyårsafton — are also included: they are not formal public holidays but are legally treated as Sundays and are de facto non-working days for commuters.

### 6.5 Partial booking (`book_partial`)

A round trip is always attempted the way the SJ app does it: one roundtrip search, one booking created from the outbound offer, return leg added to that booking via PATCH. Result: a single booking number for both legs.

- If the **return** leg has no 0-price offer (nor a close alternative), the outbound is kept and the booking is checked out outbound-only. This happens regardless of `book_partial`.
- If the **outbound** leg has no 0-price offer, nothing can be booked from the roundtrip search (the API requires an outbound offer to create a booking). With `book_partial = true` the tool then runs a one-way search for the return leg and books it as a separate booking; with `book_partial = false` the day is skipped.
- Missing legs are picked up on later runs via the duplicate check (§6.1 step 1), which searches only the missing direction as a one-way trip.

`handle_booking_process` returns `{"booking_id": ...}` when a booking was created and `None` when nothing was booked; `process_booking_flow` uses that to decide whether to run the fallback.

## 7. Departure Selection

### 7.1 Time matching

When `select_closest_ticket_available = true` (the expected default), select the departure with the smallest absolute time difference from the target time, regardless of direction (earlier or later).

When `false`, require an exact time match. If no exact match, skip that leg.

### 7.2 Class selection with fallback

When `allow_class_fallback = true` (default):

1. Try the requested `comfort_class`.
2. If unavailable, try `"2 class calm"` (unless that was the original request).
3. If unavailable, try `"2 class"` (unless that was the original request).

When a fallback occurs, **inform the user** which class was actually selected vs. what was requested.

When `allow_class_fallback = false`, only match the exact requested class. If unavailable, skip that departure.

### 7.3 Timezones

SJ API timestamps carry an explicit offset (`2026-09-01T06:59:00+02:00`). Rules, implemented once in `sj_calendar.py` (`parse_api_datetime`, `to_sweden`, `sweden_now`) and used everywhere:

- Train times, pass validity dates and config times are **Swedish wall-clock** times. API timestamps are converted to `Europe/Stockholm` before display or comparison with config values — never to the machine's local zone, so the tool behaves the same when run from abroad.
- "Is this in the past / expired / how many days left" comparisons use aware datetimes.
- A naive API timestamp (no offset) is assumed to be Swedish local time.

### 7.4 Station resolution

Station names are resolved to UIC codes via a hardcoded map in `SJClient`. This is sufficient for now. Future improvement: dynamic station lookup via the SJ API.

## 8. Edge Cases & Error Handling

### 8.1 No 0-price offer available

If a matching class+flexibility offer exists but the price is > 0, or if no matching offer is found at all:
- Log a warning with the date and direction.
- If this is the outbound leg, skip the entire date (no point booking a return without an outbound).
- If this is the return leg, keep the outbound booking and proceed to checkout as a one-way trip. Inform the user that only the outbound was booked.

### 8.2 Partial roundtrip

If the outbound books successfully but the return leg fails (no offer, API error, sold out):
- Keep the one-leg booking.
- Proceed to checkout with just the outbound.
- Inform the user clearly: `"Booked outbound only for 2026-01-20 (return leg unavailable)."`.

### 8.3 Overlapping booking conflict

If the SJ API returns an error indicating an existing booking conflicts with the requested time:
- Print a warning with the date and conflict details.
- Skip to the next date.

### 8.4 Network failures & retries

For transient HTTP errors (timeouts, 502, 503, connection resets), in `RetryTransport`:
- Idempotent requests (GET/HEAD/OPTIONS): retry up to 3 times with delays of 1s, 2s, 4s (exponential backoff).
- Non-idempotent requests (POST/PATCH — provisional booking, add leg, checkout, cancel): retry only when the request provably never reached the server (connection error / connect timeout). A 502/503 or read timeout *after* sending is not retried — the server may already have acted on it, and a retry could create a duplicate provisional booking (which would only be cleaned up on the next `--book` run, §6.2).
- After the retries are exhausted, the current operation fails and is logged; the date loop continues with the next date.
- Do not retry on 4xx errors (these are not transient).

### 8.5 Unknown API response shapes

If the API returns a JSON response that doesn't match any known structure (no `status`, `errorCode`, or `error` keys, and no expected data keys):
- Log the full response body at WARNING level.
- Treat it as an error for the current operation and continue to the next date.

### 8.6 SMS input timeout

If the user does not enter the SMS code within 2 minutes:
- Print `"sms code not provided or timed out after 2 minutes"` to stdout.
- Exit with code 1.

## 9. Travel Pass Handling

### 9.1 Auto-selection

On startup, fetch all travel passes and drop expired ones (`endTravelValidityDateTime` in the past; passes that have not started yet are kept so future dates can be booked). Behavior:
- **One valid pass**: use it automatically.
- **Multiple valid passes**: display a numbered list and prompt the user to select one.
- **No valid passes**: exit with an error.

### 9.2 Date range validation

If the travel pass has validity dates (`startTravelValidityDateTime`, `endTravelValidityDateTime`), validate that the configured `date_start` and `date_end` fall within the pass validity period. Exit with an error if not.

### 9.3 IDs

Two IDs are extracted from the travel pass:
- **Product ID** (`travelPassId`): used in search requests to indicate pass-holder pricing.
- **Passenger token**: used in booking creation. Resolved via `passengerToken` → `travelPassCreationBookingId` → fallback to product ID.

The search response may also return a `passengerListId` which takes precedence over the travel pass token for booking operations.

## 10. Output & Logging

### 10.1 Output channels

- **stdout**: user-facing status messages only. Prefixed with ` > ` via `pinfo()`. This is what the user sees at default log level.
- **stderr**: structured log output at all other levels.

### 10.2 User-facing output (stdout)

Everything the user sees goes through `sj_output.py` and prints regardless of log level:

- `pinfo()` plain message, `pdim()` dimmed context, `spinner()` progress with a dim `✓`/`✗` trail line (or none with `trail=False`), `print_day_header()` / `print_day_note()` / `print_leg_lines()` for cards, `indented()` to nest everything printed inside a block under a day header.
- **Casing convention**: prose is lowercase (`no departure found for outbound`, `✓ checking offers…`), identifiers keep their case — booking numbers upper-case (`ERU0HWB2`), station names as SJ writes them (`Linköping Central`), train names as given. Titles (`🎫 sj årskort silver`, the context line) are lowercase. Nothing is lowercased automatically any more; `pinfo` prints what it is given.
- Structure is the same in every mode: context line (`name · email · pass`), dim progress trail, cards, dim summary footer. Emoji only in titles and footers, never inside aligned rows. Colour/bold/dim only on a TTY without `NO_COLOR`.
- Prompts (`input()`) accept `y`/`yes` for confirmation, any case.

### 10.3 Log levels (stderr)

| Level | Content |
|---|---|
| CRITICAL | Default (silent). Only fatal unrecoverable errors. |
| ERROR | API errors, failed bookings, auth failures. |
| WARNING | Skipped dates, class fallbacks, unknown response shapes. |
| INFO | Per-date progress, selected departures, offer details. |
| DEBUG | HTTP request/response summaries, config values, token state. |
| TRACE | Full httpx/httpcore internals (request headers, response bodies). |

### 10.4 Log format

Structured key-value format on stderr:
```
time=2026-01-19T08:30:15.123+01:00 level=INFO     file=sj_tool.py          class=SJTool              function=process_booking    msg="Selected outbound: 05:29 (diff: 0m)"
```

Color-coded by level in terminal.

## 11. Removed / Out of Scope

- **Ad-hoc trip booking**: different routes per date, multi-stop journeys.
- **Multi-passenger booking**: always single passenger (the pass holder).
- **Dynamic station lookup**: use hardcoded map for now.
- **SMS retry / re-trigger**: user re-runs the tool if SMS doesn't arrive.
- **`dry_run` config key**: removed from config. Dry-run is the default CLI mode; `--book` books for real.
- **Interactive confirmation before booking**: the config is the contract. The tool books what's configured.

## 12. Dependencies

- Python 3.13+
- `httpx` — HTTP client
- `typing_extensions` — `@override` decorator
- No `requirements.txt` or `pyproject.toml`. Manual install: `pip install httpx typing_extensions`
- `pytest` (dev only) — unit tests in `tests/`, no network; `pytest.ini` sets `pythonpath = .`
