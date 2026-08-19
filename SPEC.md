# SPEC.md

Specification for the SJ API Client — a Python CLI tool that reverse-engineers sj.se to automatically book daily commute train tickets using an annual travel pass (e.g., SJ Årskort).

## 1. Purpose

Book train tickets for a daily commute over a date range (typically 1–3 months) using the 0-price benefit of an SJ travel pass. The tool authenticates via SJ's Azure AD B2C flow, searches for departures matching the user's time preferences, and books the best available option for each date.

This is **not** a general-purpose booking tool. It handles one route, one passenger (the pass holder), repeated across a date range.

## 2. Architecture

Plain venv, no package manager or build system. Modules organized by separation of concerns:

| Module | Role |
|---|---|
| `sj_tool.py` | Entry point. CLI argument parsing (`--dry-run`, `--cancel-date`, `--list-current-bookings`), top-level orchestration. |
| `sj_auth.py` | Authentication orchestration. B2C login flow, token lifecycle (validate → refresh → full login), SMS input with timeout. |
| `sj_client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic. Includes HTTP retry logic. |
| `sj_booking.py` | Booking business logic. Search, departure selection, offer matching, provisional booking creation, checkout, cancellation, duplicate detection. |
| `sj_config.py` | Configuration loading and validation (`CfgManager`). |
| `sj_token.py` | Token cache management (`TokenManager`). Load, save, validate expiry, check refresh availability. |
| `sj_logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. |
| `sj_errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`). |
| `sj_output.py` | User-facing output helpers. `pinfo()`, table formatting for dry-run results and booking listings. |

### Design principles

- **Single responsibility**: each module has one job. The HTTP client doesn't decide what to book. The booking logic doesn't manage tokens.
- **No business logic in the client**: `sj_client.py` exposes methods like `search_journey()`, `get_offers()`, `create_provisional_booking()`. It does not interpret results or make decisions.
- **Thin entry point**: `sj_tool.py` parses CLI args, wires modules together, and delegates. The main loop body calls into `sj_booking.py`.
- **Errors as exceptions**: use typed exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) rather than return-code checking. Catch at the orchestration layer.

### Data flow

```
CLI args (--dry-run, --cancel-date, --list-current-bookings)
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
  --list-current-bookings → fetch & display bookings table → exit
  --cancel-date DATE      → find bookings → interactive cancel → exit
  --dry-run               → search loop → display results table → exit
  (default)               → booking loop:
                              clean up stale provisionals
                              for each date in range:
                                sj_booking: check duplicates
                                sj_booking: search → select → offer → book → checkout
                              summary → exit
```

## 3. Authentication

### 3.1 Token lifecycle

Follow industry-standard OAuth2 token handling:

1. **Load cached token** from `~/.cache/sj_tool/token.json`.
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

`~/.config/sj_tool/config.toml` (or `$XDG_CONFIG_HOME/sj_tool/config.toml`)

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
allow_class_fallback = true
skip_weekends = true
skip_holidays = true
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
| `skip_weekends` | Optional. Boolean. Defaults to `true`. Skip Saturdays and Sundays. |
| `skip_holidays` | Optional. Boolean. Defaults to `true`. Skip Swedish red days (see §6.4). |

Report all validation errors at once (don't stop at the first one).

### 4.4 Token cache

`~/.cache/sj_tool/token.json` — auto-created on first successful login. Contains `access_token`, `refresh_token`, `expires_on`, etc.

## 5. CLI Interface

### 5.1 Normal mode (book)

```bash
python3 sj_tool.py
```

Reads config, authenticates, and books tickets for every date in the configured range. No confirmation prompt — the config is the source of truth.

### 5.2 Dry-run mode

```bash
python3 sj_tool.py --dry-run
```

Performs the full search flow for each date but does **not** create bookings. Instead, prints a summary table showing what *would* be booked:

```
Dry Run Results
───────────────────────────────────────────────────────────────────────────
 Date        Direction   Departure  Arrival   Class          Flexibility
───────────────────────────────────────────────────────────────────────────
 2026-01-19  Outbound    05:29      08:15     2 class calm   FULLFLEX
 2026-01-19  Return      17:22      20:08     2 class calm   FULLFLEX
 2026-01-20  Outbound    05:29      08:15     2 class calm   FULLFLEX
 2026-01-20  Return      17:22      20:08     2 class calm   FULLFLEX
 2026-01-21  Outbound    —          —         —              — (no 0-price offer)
 2026-01-21  Return      17:22      20:08     2 class calm   FULLFLEX
───────────────────────────────────────────────────────────────────────────
```

### 5.3 Cancel mode

```bash
python3 sj_tool.py --cancel-date 2026-01-20
```

Interactive cancellation for the specified date on the configured route:

1. Look up existing bookings for that date + route.
2. If a roundtrip exists, ask the user:
   ```
   Found bookings for 2026-01-20 (Linköping Central → Stockholm Central):
     1. Outbound  05:29 → 08:15
     2. Return    17:22 → 20:08
     3. Both

   Cancel which? [1/2/3]:
   ```
3. After selection, confirm:
   ```
   Cancel outbound 2026-01-20 05:29 Linköping → Stockholm? [y/N]:
   ```
4. Only proceed on explicit `y`.

If only one leg exists, skip the direction question and go straight to confirmation.

### 5.4 List current bookings

```bash
python3 sj_tool.py --list-current-bookings
```

Fetches all active bookings within the travel pass validity period and displays them in a table:

```
Current Bookings (SJ Årskort Silver)
─────────────────────────────────────────────────────────────────────────────────────────
 Date        Direction  Departure  Arrival  Duration  Class          Route
─────────────────────────────────────────────────────────────────────────────────────────
 2026-01-19  Outbound   05:29      08:15    2h 46m    2 class calm   Linköping C → Stockholm C
 2026-01-19  Return     17:22      20:08    2h 46m    2 class calm   Stockholm C → Linköping C
 2026-01-20  Outbound   05:29      08:15    2h 46m    2 class calm   Linköping C → Stockholm C
 2026-01-20  Return     17:22      20:08    2h 46m    2 class calm   Stockholm C → Linköping C
 2026-01-21  Outbound   06:29      09:15    2h 46m    2 class        Linköping C → Stockholm C
─────────────────────────────────────────────────────────────────────────────────────────
 5 bookings shown.
```

- Only shows non-cancelled bookings.
- Sorted by date, then direction (outbound before return).
- Pagination: fetches all pages from the bookings API.
- No booking modifications — read-only mode.

### 5.5 Environment variables

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | Set log verbosity: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default: `CRITICAL` (silent). |

### 5.6 CLI flag summary

| Flag | Description | Interactive? |
|---|---|---|
| *(none)* | Book tickets for the configured date range. | Only SMS on first login. |
| `--dry-run` | Search and display what would be booked, without booking. | Only SMS on first login. |
| `--cancel-date YYYY-MM-DD` | Cancel bookings for a specific date. | Yes (direction choice + confirmation). |
| `--list-current-bookings` | Display all active bookings in a table. | Only SMS on first login. |

Flags are mutually exclusive. Specifying more than one is a config error (exit 1).

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

On startup (after auth, before the booking loop), fetch all existing bookings in the date range. Any booking with status `"NEW"` and `"CANCEL_JOURNEY"` in `possibleActions` is a stale provisional booking from a previous interrupted run. Cancel these automatically. This always runs — it is not opt-in.

### 6.3 Timing between dates

2-second delay between processing each date to avoid hammering the API. No evidence of rate limiting at this interval over 1–3 month ranges. Skipped dates (§6.4) do not incur the delay.

### 6.4 Weekend and holiday skipping

Implemented in `sj_calendar.py` with no external dependency. `skip_reason(date, skip_weekends, skip_holidays)` returns `"weekend"`, the holiday name, or `None`.

Swedish red days are computed per year: fixed dates (Nyårsdagen, Trettondedag jul, Första maj, Nationaldagen, Juldagen, Annandag jul), Easter-derived (Långfredagen, Påskdagen, Annandag påsk, Kristi himmelsfärdsdag, Pingstdagen; Easter via the Meeus/Jones/Butcher algorithm) and window-based Saturdays (Midsommardagen = Saturday in June 20–26, Alla helgons dag = Saturday in Oct 31–Nov 6). The three eves — Midsommarafton, Julafton, Nyårsafton — are also included: they are not formal public holidays but are legally treated as Sundays and are de facto non-working days for commuters.

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

### 7.3 Station resolution

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

For transient HTTP errors (timeouts, 502, 503, connection resets):
- Retry up to 3 times with delays of 1s, 2s, 4s (exponential backoff).
- After 3 failures, skip the current operation and log the error.
- Do not retry on 4xx errors (these are not transient).

### 8.5 Unknown API response shapes

If the API returns a JSON response that doesn't match any known structure (no `status`, `errorCode`, or `error` keys, and no expected data keys):
- Log the full response body at WARNING level.
- Treat it as an error for the current operation and continue to the next date.

### 8.6 SMS input timeout

If the user does not enter the SMS code within 2 minutes:
- Print `"SMS code input timed out after 2 minutes."` to stdout.
- Exit with code 1.

## 9. Travel Pass Handling

### 9.1 Auto-selection

On startup, fetch all travel passes. Filter to only active/valid passes. Behavior:
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

### 10.2 User-facing messages (stdout via pinfo)

These print regardless of log level:

```
 > authenticating ...
 > logged in as John Doe
 > travel pass: SJ Årskort Silver (valid 2026-01-01 to 2026-12-31)
 > cleaning up 2 stale provisional bookings ...
 > processing date 2026-01-19 (1/45) ...
 > booked outbound 05:29→08:15 (2 class calm, FULLFLEX)
 > booked return 17:22→20:08 (2 class calm, FULLFLEX)
 > note: class fallback on 2026-01-20 outbound — requested "1 class", booked "2 class calm"
 > warning: no 0-price offer for 2026-01-21 outbound — skipping date
 > warning: return leg unavailable for 2026-01-22 — booked outbound only
 > done. 43 of 45 dates booked successfully, 2 dates skipped.
```

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
- **`dry_run` config key**: removed from config. Dry-run is a CLI flag (`--dry-run`) only.
- **Interactive confirmation before booking**: the config is the contract. The tool books what's configured.

## 12. Dependencies

- Python 3.13+
- `httpx` — HTTP client
- `typing_extensions` — `@override` decorator
- No `requirements.txt` or `pyproject.toml`. Manual install: `pip install httpx typing_extensions`
