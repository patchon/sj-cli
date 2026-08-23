# SPEC.md

Specification for the SJ API Client — a Python CLI tool that reverse-engineers sj.se to automatically book daily commute train tickets using an annual travel pass (e.g., SJ Årskort).

## 1. Purpose

Book train tickets for a daily commute over a date range (typically 1–3 months) using the 0-price benefit of an SJ travel pass. The tool authenticates via SJ's Azure AD B2C flow, searches for departures matching the user's time preferences, and books the best available option for each date.

This is **not** a general-purpose booking tool. It handles one route, one passenger (the pass holder), repeated across a date range.

## 2. Architecture

Standard src layout declared in `pyproject.toml` (PEP 621, `pip install -e .`). Modules organized by separation of concerns:

| Module | Role |
|---|---|
| `cli.py` | Entry point. CLI argument parsing (see §5.6), top-level orchestration. |
| `auth.py` | Authentication orchestration. B2C login flow, token lifecycle (validate → refresh → full login), SMS input with timeout. |
| `client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic. Includes HTTP retry logic. |
| `booking.py` | Booking business logic. Search, departure selection, offer matching, provisional booking creation, checkout, cancellation, duplicate detection. |
| `config.py` | Configuration loading and validation (`CfgManager`). |
| `tokens.py` | Token cache management (`TokenManager`). Load, save, validate expiry, check refresh availability. |
| `logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. |
| `errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) and `error_text()`, the one-line, redacted rendering every printed exception goes through. |
| `output.py` | User-facing output helpers. `pinfo()`/`pdim()`, ANSI styling, spinner, per-day booking cards, status cards (auth modes), travel-pass cards. |
| `dates.py` | Swedish red-day calendar (Easter computed, no dependency). `skip_reason()` for weekend/holiday skipping. |

### Design principles

- **Single responsibility**: each module has one job. The HTTP client doesn't decide what to book. The booking logic doesn't manage tokens.
- **No business logic in the client**: `client.py` exposes methods like `search_journey()`, `get_offers()`, `create_provisional_booking()`. It does not interpret results or make decisions.
- **Thin entry point**: `cli.py` parses CLI args, wires modules together, and delegates. The main loop body calls into `booking.py`.
- **Errors as exceptions**: use typed exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) rather than return-code checking. Catch at the orchestration layer.

### Data flow

```
CLI args (see §5.6)
  ↓
config.toml → config.CfgManager → validate all fields
  ↓
token.json → tokens.TokenManager → valid? → use access_token
  ↓ expired?                           ↓ no token?
auth: refresh_token                auth: full B2C login (interactive SMS)
  ↓ fail?                              ↓
auth: full B2C login → save tokens
  ↓
Mode dispatch:
  --list-bookings         → fetch & display per-day booking cards → exit
  --list-travelpasses     → fetch & display travel-pass cards → exit
  --cancel-date DATES     → per date: find bookings on route → interactive cancel → exit
  --cancel-booking NUMS   → find bookings by number → interactive cancel → exit
  --login                 → exit after auth
  --logout                → end B2C session, delete caches → exit (skips config/auth)
  (--dry-run modifies --book / --cancel-*: preview only, nothing mutated)
  --book                  → booking loop:
                              clean up stale provisionals
                              for each selected date:
                                dates: skip weekends / red days
                                booking: check duplicates
                                booking: search → select → offer → book → checkout
                                per-day result line
                              exit
```

## 3. Authentication

### 3.1 Token lifecycle

Follow industry-standard OAuth2 token handling:

1. **Load cached token** from `~/.cache/sj-api-client/token.json`.
2. **Validate access token**: check `expires_on` timestamp with a 5-minute safety buffer. If valid, use it.
3. **Refresh**: if access token is expired but refresh token exists, call the B2C token endpoint with `grant_type=refresh_token`. On success, cache the new token set and proceed. The refresh POST is retried like a read (replaying it is harmless). A *transient* failure — network error, timeout, 5xx — is reported as `● token refresh failed: …` and the run exits 1 (re-run later; the cache is untouched); only a refresh token that B2C rejects (a 4xx with an error body) falls through to the full login.
4. **Full login**: if no cached token exists, or refresh fails, perform the interactive B2C login flow. This is the only path that requires user interaction.

The tool must always attempt to use what it has before falling back to a more intrusive method. Never prompt for SMS if a valid or refreshable token exists.

### 3.2 B2C login flow

Multi-step sequence against `id.sj.se` (Azure AD B2C):

1. Fetch login page → extract CSRF token and transaction ID from HTML/cookies. If B2C instead redirects straight back to the sj.se callback with an authorization code (a live SSO session, although the silent login of §3.1 did not succeed — e.g. its token exchange failed), steps 2–9 are skipped and that code goes to step 10.
2. Submit email + password credentials.
3. Confirm login stage with provider.
4. Send device fingerprint. B2C reports a rejected registration (stale CSRF token, bad transaction) as a JSON error with HTTP 200; it is raised as an `SJAPIError` with B2C's code and message here, not discovered one request later.
5. Advance to MFA orchestration step.
6. Trigger SMS MFA.
7. Prompt user for SMS code (stdin). **Timeout: 2 minutes.** If no input within 2 minutes, print an error and exit. An empty line (an Enter pressed while the spinners ran) is not an answer: `! no code entered, try again`, and the prompt returns. The timeout applies at a terminal; piped input is read line by line until end of input.
8. Verify SMS code. A rejected code (B2C answers a bare `{"status": "449"}`, its "retry" signal) is re-prompted — 3 attempts in total against the same SMS, which is never re-sent. After the third rejection the run fails with `sms code rejected 3 times, re-run to try again`.
9. Finalize login → extract authorization code from redirect.
10. Exchange authorization code for tokens (access, refresh, id token) via PKCE.

No SMS re-send mechanism: if the code doesn't arrive, the user re-runs the tool. A mistyped code, however, is re-prompted as per step 8.

### 3.3 Mid-run token expiry

Access tokens live 15 minutes and the validity check keeps a 5-minute buffer, so any run longer than ~10 minutes refreshes mid-run — routine, not an edge case. The refresh is transparent. If it fails, that day's card shows `error: …` and `! stopping: no valid session for the remaining dates`, the summary line is still printed (the day counts as an error) and the run exits 1 — a transient failure says `token refresh failed: …` (re-run later), a rejected refresh token asks for a re-run to re-authenticate.

### 3.4 Credentials

Email and password stored in plaintext in `config.toml`. Acceptable for this use case.

## 4. Configuration

### 4.1 File location

`~/.config/sj-api-client/config.toml` (or `$XDG_CONFIG_HOME/sj-api-client/config.toml`)

**First run**: when the file does not exist, `--login` (and only `--login` — every other operation presupposes a session) run in a terminal offers to create it — `? create it now? [y/n]`, then asks for the SJ email (validated, re-asked) and password (via `getpass`, never echoed, re-asked if empty), writes the documented template (`config.example.toml`) with the credentials filled in (TOML-escaped), chmods it to `0600`, and closes with a `● config created` card pointing at `[search_parameters]`; the run then **continues into the login**. The offer requires both stdin and stdout to be terminals (the prompts go to stdout). Declined: a `● no configuration` card that only points back at `--login` (the wizard already named the path); non-interactive or any other operation: the card names the expected path and points at `--login`; exit 1 in all cases. Ctrl-D at the password prompt counts as an empty answer and is re-asked; a write failure (permissions, a file where the directory should be) shows `● config not created` with the OS error, exit 1. A missing file is reported as missing, an unreadable one as `cannot read config` — `failed to parse` is reserved for real TOML errors.

### 4.2 Schema

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"     # or e.g. "W36, W38..40"
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

Validation runs at startup before any API calls, scoped to the operation: `[auth]` is always validated; `[search_parameters]` only for the operations that use it (`--book`, `--cancel-date`) — login, listing and cancel-by-number work with a config holding only credentials (e.g. one freshly written by the first-run setup) — and the `dates` selection only for `--book`: `--cancel-date` takes its own dates and needs just the route. Fail fast: the errors render as a status card — `● invalid configuration` (red dot + bold verdict), a blank line, then one plain indented line per error — and the run exits 1. `SJConfigError` carries the individual messages as `.errors`; file-level failures (missing/unparsable config) render the same card with a single line.

| Field | Rules |
|---|---|
| `email` | Required. Must match basic email format (`x@y.z`). |
| `password` | Required. Non-empty string. |
| `dates` | Required for `--book`. A string: comma-separated units and inclusive `START..END` ranges, mixed freely. A unit is a date (`2026-09-14`) or an ISO week — `W43` (week 43 of today's ISO year, Swedish date; around New Year write the year) or `2027-W02`. A week expands to Monday..Sunday; the end of a week range may omit year and `W` (`W43..46`, `2027-W02..03`, inheriting the start's year); both ends of a date range are full dates. Ranges must run forwards and span ≤ 1 year; a range may not mix a date and a week; `W53` only in years that have it. The expanded dates deduplicate; the written value is normalised in place (terms trimmed, `w` upper-cased; spaces around `..` are accepted and kept). Dates already gone are skipped at run time (`! 7 selected day(s) have passed, starting from …`); a selection that has passed entirely is an error (`dates: all selected dates have passed`); empty → `dates is required`; a native (unquoted) TOML date → `dates must be a string like "2026-09-01..2026-10-30"`. Grammar errors are prefixed `dates:` and echo the value, e.g. `dates: '2026-09-31' is not a real calendar date`, `dates: 'foo' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)`, `dates: range 'W44..43' must run forwards (start before end)`, `dates: 2027 has no week 53` (the full list is in `dates.parse_date_selection`). A config still using `date_start`/`date_end` gets `date_start/date_end were replaced by dates = "START..END"`. |
| `time_leave` | Required. Quoted `"HH:MM"` string (24-hour) or native (unquoted) TOML time with seconds (`06:59:00`) — both accepted, normalised to `HH:MM`. Errors echo the received value and distinguish a wrong format from an impossible time of day. |
| `time_return` | Required if `roundtrip = true`. Same time rules as `time_leave`. |
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

`~/.cache/sj-api-client/token.json` — auto-created on first successful login. Contains `access_token`, `refresh_token`, `expires_on`, etc. SSO cookies for silent re-login are cached next to it in `cookies.json`. Both are deleted by `--logout`.

## 5. CLI Interface

### 5.1 Book mode

```bash
sj-tool --book
```

Reads config, authenticates, and books tickets for every selected date (`dates`, §4.3). No confirmation prompt — the config is the source of truth. Output opens with the **header box** (`print_header_box`): a rounded dim-bordered box holding dim-labelled rows — `operation` (bold value, `booking tickets`; dry run prefixes `dry run · `), `account` (config email), `travelpass` and `holder` (real casing) — then a blank line and the run's config as dim-labelled facts in the shared card grammar: `route`, `days` (span + day filter, e.g. `weekdays only` — a contiguous selection renders as `1 sep – 30 oct 2026`, anything else as `W43, W45..46 (19 oct – 15 nov 2026)`), `times`, and `ticket` (class, flexibility, train filter, and any non-default switches such as `exact time only`, `no class fallback`, `partial ok`). Then one **day card** per date (the same card shape as `--list-bookings`, §5.4): a bold date + route header, the progress trail and any messages indented beneath it, then the booked legs, and a blank line. Days that need no work are a single line (bold date + dim reason). The run closes with a status line (`pstatus`): green/red ● by outcome, dim summary text.

```
╭──────────────────────────────────╮
│  operation    booking tickets    │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       John Doe           │
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
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 klass Lugn   ERU0HWB2
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 66   2 klass Lugn   ERU0HWB2

wed 16 sep 2026   tickets already booked

sat 19 sep 2026   weekend

thu 24 sep 2026   Göteborg Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  no departure found for outbound
  nothing booked

● 4 day(s) · 1 booked · 1 already booked · 1 not booked · 2 skipped
```

The booked legs are rendered from the booking object the API returns (train, carriage/seat, class, booking number), so they are identical to what `--list-bookings` shows afterwards. A checkout failure prints `checkout failed, provisional left (cleaned up on next --book run)` and counts as `checkout failed` in the footer; an exception while processing a day prints `error: …` and counts as `error(s)`. Either turns the closing `●` red and makes the run exit 1 (§5.7); days without a 0-price offer are skips (`not booked`), not failures. The route in the header shows what the day needs: `A ⇄ B` both legs, `A → B` outbound only, `B → A` return only (with a `return already booked, searching outbound only` / `outbound already booked, searching return only` line beneath).

### 5.2 Dry-run mode

```bash
sj-tool --book --dry-run
```

`--dry-run` is a **modifier**, not an operation: it composes with `--book`, `--cancel-date` and `--cancel-booking`, and means nothing real happens. Bare `--dry-run`, or `--dry-run` with any other flag, is a usage error. With `--book` it performs the full search flow for each date but does **not** create bookings (and skips the stale-provisional cleanup). Same run header and day cards as §5.1, with the operation prefixed `dry run · `; each leg line shows the departure that would be booked — time range, duration, train, class, flexibility — or, dimmed in place of the flexibility cell, why it cannot be booked (`no 0-price offer`, `no departure found`). The footer starts with `dry run` and counts bookable / partly bookable / unavailable days.

```
╭──────────────────────────────────────────╮
│  operation    dry run · booking tickets  │
│  account      user@example.com           │
│  travelpass   SJ Årskort Silver          │
│  holder       John Doe                   │
╰──────────────────────────────────────────╯

  route     Göteborg Central ⇄ Stockholm Central
  days      18 – 21 sep 2026 · weekdays only
  times     out 06:59 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

fri 18 sep 2026   Göteborg Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  ✓ checking offers for outbound at 06:59
  ✓ searching return at 17:22
  ✓ checking offers for return at 17:22
  → 06:59 – 10:04   3h 05m   X 2000 520   2 class calm   FULLFLEX
  ← 17:22 – 20:28   3h 06m   X 2000 543   2 class calm   no 0-price offer

sat 19 sep 2026   weekend

● dry run · 2 day(s) · 1 partly bookable · 1 skipped
```

### 5.3 Cancel mode

```bash
sj-tool --cancel-date 2026-01-20              # all bookings on the configured route that day
sj-tool --cancel-date 2026-01-20,2026-02-03..2026-02-05   # several dates, comma list + ranges
sj-tool --cancel-date W43                      # a whole ISO week
sj-tool --cancel-booking ERU0HWB2,8Y41N08J   # by booking number, any case
```

For each matching booking: show its day card (same shape as §5.4, no title), then confirm. `y`/`yes` (any case) confirms; anything else aborts. `--cancel-date` cancels only that day's journeys: a booking made on sj.se may hold journeys on other days, which are neither shown nor touched — `1 other journey in booking X on other dates is kept` under the card, the prompt reads `cancel this journey from booking X?`, and the status line `1 of 2 journey(s) cancelled from booking X`.

```
╭────────────────────────────────────╮
│  operation    cancelling bookings  │
│  account      user@example.com     │
│  travelpass   SJ Årskort Silver    │
│  holder       John Doe             │
╰────────────────────────────────────╯

✓ searching for booking ERU0HWB2

tue 15 sep 2026   Göteborg Central → Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 klass Lugn   ERU0HWB2

? cancel booking ERU0HWB2? [y/n]: y
✓ cancelling booking ERU0HWB2

● booking ERU0HWB2 cancelled
```

If the booking has several future journeys, they are listed as numbered leg lines (`1.`, `2.`, … and `a.` for all); the selected legs are echoed as leg lines under `selected for cancellation:` before the final `? cancel selected journey(s)? [y/n]:`. All cancel prompts (`? select [1/2/a]:`, the `[y/n]` confirmations) use the shared inline `?`-marked prompt (`ask()` in `output`). Past journeys are shown but cannot be selected. A booking with a pending cancellation offers confirm / revert / nothing. With `--dry-run`, the day cards are shown and the run stops there — no prompts, no cancellation calls — closing with `● dry run · N journey(s) would be cancelled from booking X` per booking (or `● dry run · booking X has a pending cancellation, nothing done`).

### 5.4 List current bookings

```bash
sj-tool --list-bookings
```

Fetches all active bookings within the travel pass validity period and displays them as one card per travel day, legs indented beneath:

```
tue 18 aug 2026   Göteborg Central ⇄ Stockholm Central   past
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 34   2 klass Lugn   ZR8C6RT1
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 22   2 klass Lugn   ZR8C6RT1

wed 19 aug 2026   Göteborg Central ⇄ Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 30   2 klass Lugn   TBRS43MG
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 55   2 klass Lugn   TBRS43MG

mon 31 aug 2026   Göteborg Central ⇄ Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 7 seat 32   2 klass        3RK7YJU4
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 21   2 klass Lugn   K883DH2T

● 3 day(s) · 4 booking(s) · 2 in the past
```

- Header per day: date, route (`A ⇄ B` when both directions are present, otherwise the distinct routes), and a `past` tag when every leg has departed.
- Leg line: direction arrow, departure – arrival, duration, train (brand + number), carriage/seat, class, booking number. The arrow is inferred from the route (reverse of the day's first leg → `←`) because the API reports a standalone return booking as `OUTBOUND`.
- Grouped by date rather than booking number: a return leg booked via the `book_partial` fallback (§6.5) is its own booking, so the booking number is shown per leg. Legs are sorted by departure time.
- A day whose legs have all departed is dimmed; when colour is unavailable a `past` tag is appended instead.
- Only shows non-cancelled bookings. Pagination: fetches all pages from the bookings API. Read-only.
- Opens with the header box (`operation   listing bookings` + account/travelpass/holder), then the day cards; the bookings fetch is a silent spinner. The closing status line (green ●, dim text) counts days and bookings (leg count omitted — the legs are visible in the cards), plus `N in the past` when applicable. Elsewhere, progress steps that matter leave a dimmed trail line when done — `✓ searching outbound at 06:59`, or `✗ …` if the step raised — so logs show where time went or where a step failed. When stdout is not a TTY only the trail line is printed.
- Styling: bold header/booking numbers, dimmed past days, coloured arrows. ANSI colour is emitted only when stdout is a TTY and `NO_COLOR` is unset (`TERM=dumb` also disables it); piped output is plain text. Emoji appear only in run-mode title lines, never in footers or aligned rows.
- The card renderer (`day_header`, `leg_lines` in `output.py`) is shared by list, book, dry-run and cancel output; columns that are empty on every row of a card are omitted, so book/list cards show train · seat · class · number while dry-run cards show train · class · flexibility/note. Travel passes render as cards in the same grammar (`print_travelpasses`): bold pass name + card number as the header, then dim-labelled facts (holder, valid with days left, price from the receipt); unknown facts are omitted, and a `● N travel pass(es)` status line closes the mode.

### 5.5 Environment variables

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | Set log verbosity: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default: `CRITICAL` (silent). |
| `NO_COLOR` | If set (any value), disable ANSI colour/bold in output. Colour is also disabled automatically when stdout is not a TTY. |

### 5.6 CLI flag summary

| Flag | Description | Interactive? |
|---|---|---|
| `--dry-run` | Modifier for `--book`/`--cancel-date`/`--cancel-booking`: preview only, nothing booked or cancelled, no prompts. Usage error alone or with any other flag. | Only SMS on first login. |
| `--book` | Book tickets for the configured dates (`dates`, §4.3). | Only SMS on first login. |
| `--cancel-date DATES` | Cancel bookings on the configured route for one or more dates: a `YYYY-MM-DD` date, an ISO week (`W43`, `2027-W02`; a bare week is this ISO year), a comma-separated list, and/or inclusive `START..END` ranges, mixed freely. Every token is validated up front (the same grammar as the config's `dates` key, §4.3); any problem renders an `● invalid --cancel-date` card echoing each bad value and exits 1 before any API call. Dates are deduplicated and processed in order, one section per date. | Yes (journey choice + confirmation). |
| `--cancel-booking NUM[,NUM…]` | Cancel booking(s) by booking number, comma-separated, any case (deduplicated, order kept). Validated up front: a value that cannot be a booking number (anything beyond letters and digits) or that looks like a date or week (`2026-09-16`, `W43`, `2027-W02`, a `..` range) renders an `● invalid --cancel-booking` card echoing each bad value — with a `did you mean --cancel-date?` hint for the date/week-shaped ones — and exits 1 before any API call. One output section per booking, blank-line separated. | Yes (journey choice + confirmation). |
| `--list-bookings` | Display all active bookings as per-day cards. | Only SMS on first login. |
| `--list-travelpasses` | Display travel passes with validity and receipt details. | Only SMS on first login. |
| `--login` | Header box (`operation   logging in` + config email), auth trail, then the login-status card. Verdict `● logged in` when a full login ran, `● already logged in` when the cached/refreshed session sufficed. The card judges the session just established, not the cache file; a cache that could not be written is said first (`! token cache not saved: … · the next run will need to log in again`). | Only SMS on first login. |
| `--logout` | End the sj.se SSO session and delete the cached token and cookies. | No. |
| `--login-status` | Exit 0 if logged in (valid or refreshable cached token), else 1. No network. | No. |

Only the documented flags exist — no hidden aliases; renamed flags reject their old spellings with a usage error, and flag prefixes are not accepted either (`--logo` is `unrecognized arguments`, not `--logout`). An empty value (`--cancel-date ""`) is an invalid argument, not a missing operation.

`--login-status` judges from the cache alone, using the same test the auth flow uses for its non-interactive path (§3.1 steps 2–3): exit 0 when the access token is valid **or** a refresh token is still usable. Cached SSO cookies are not considered — they cannot be verified without a network call. Output opens with the session-scoped **header box** (`operation   checking login status`, plus `account` — the email from the token's base64url `profile_info` blob, `preferred_username` claim, row omitted if undecodable), then the **status card**: a coloured dot (green/red) + bold verdict, a blank line, then dim-labelled facts:

```
╭───────────────────────────────────────╮
│  operation   checking login status    │
│  account     user@example.com         │
╰───────────────────────────────────────╯

● logged in

  session   valid until fri 22 aug 18:04 (expires in 23h)
  token     expired thu 21 aug 21:11 · renews automatically on next run
```

`session` is the horizon in Swedish wall-clock time with a coarse relative distance — the refresh-token expiry (extended automatically by every run), or the access-token expiry if there is no refresh token, or `renews on next run` if unknown. `token` shows the access-token expiry, `valid until …` or `expired … · renews automatically on next run`. Not logged in: the red-dot card with `session   expired · log in with --login` or `session   no cached login found · log in with --login`.

Flags are mutually exclusive, and one mode flag is required — there is no implicit default mode. Every usage error (no flag, unknown flag/argument, conflicting flags) prints the **full help** followed by a red `●` status line naming the problem, and exits 1. Help output (also via `-h`) always ends with a blank line.

`--logout` needs no config: it calls the B2C OIDC end-session endpoint (`{URL_AUTH_FLOW_BASE}/oauth2/v2.0/logout`, authenticated by the cached SSO cookies) and deletes `token.json` and `cookies.json`. Output: a header box (`operation   logging out`, plus `account` from the cached token's profile_info when available — all offline), then trail steps (`✓ ending sj.se session` when cookies existed, `✓ removing cached token and cookies` when caches existed), then a bare verdict card — `● logged out`, or `● already logged out` when there was nothing to do (server call skipped). The local caches are cleared even when the server call fails, but that failure renders `● logout failed` with the error line and exits 1. After a logout the next login requires the SMS step again.

### 5.7 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Any failure: config or auth error; a `--book` run in which any day's checkout failed or errored; a `--cancel-*` run in which any cancellation was refused by the API, declined at a prompt, or (`--cancel-booking`) the number was not found. A day with no offer, or a `--cancel-date` date with nothing to cancel, is not a failure. |

Every failure that ends a run closes with a red `●` status line naming the cause (`● initialization failed: …`, `● error: …`, `● token refresh failed: …`, `● no valid travel pass found`). Error texts are one line (httpx's "for more information" line is dropped), never empty (an exception without a message shows its type) and never carry an auth code from a URL.

## 6. Booking Workflow

### 6.1 Per-date flow

For each selected date (`dates`, §4.3) from today on:

0. **Calendar filter**: if the date is a weekend (`skip_weekends`) or a Swedish red day (`skip_holidays`), print the one-line day note (bold date + dim reason, `sat 19 sep 2026   weekend`) and continue to the next date without any API calls (see §6.4). Dates before today are not walked at all: they are dropped with one note (`! 7 selected day(s) have passed, starting from 2026-10-21`); if every selected day has passed by the time the loop runs, the run ends with `● all selected dates have passed` (exit 1). Unselected days between selected ones print nothing.
1. **Duplicate check**: fetch existing bookings (from today to the end of the pass, so a selection starting today is covered) and check whether an active booking — not cancelled, not a stale provisional — already has a journey with this route's end points on this Swedish date (a journey with a change matches by its end points, not per segment). If fully booked (both legs for roundtrip, or single leg for one-way), skip with an info message.
2. **Determine what to book**: outbound only, return only, or both — based on which legs are missing.
3. **Search**: call `search_journey` with the appropriate parameters. For roundtrip where both legs are needed, use a single roundtrip search. For single missing legs, use a one-way search.
4. **Poll results**: poll `get_search_results` up to 5 times with 1-second intervals until departures appear.
5. **Select departure**: pick the best departure based on time preference and class availability (see §7).
6. **Get offers**: fetch offers for the selected departure.
7. **Find 0-price offer**: locate an offer matching the requested class + flexibility with `amount == 0`. If not found, warn and skip (see §8).
8. **Create provisional booking**: create the booking with the outbound offer.
9. **Add return leg** (if roundtrip): get offers for the return departure and add to the existing booking via PATCH.
10. **Update customer details**: set the config email and the phone number the API already placed on the provisional (`customer.phoneNumber` of the create response, the passenger's as a fallback). Never a placeholder: with no number on the booking the field is left out.
11. **Checkout**: finalize the booking.

### 6.2 Provisional booking cleanup

On startup (after auth, before the booking loop), fetch all existing bookings in the date range. A booking with status `"NEW"` and `"CANCEL_JOURNEY"` in `possibleActions` is a provisional. Only the ones that look like this tool's own leftovers are cancelled: a journey on the configured route (either direction) **and** `created` more than 10 minutes ago (`STALE_PROVISIONAL_GRACE`). A cart the user has open on sj.se for another trip is never touched (or mentioned); a younger provisional on the route — a checkout in progress, the user's or a concurrent run's — is left alone with `! leaving recent provisional booking X alone (created 2m ago)`. A provisional on the route without a usable `created` timestamp counts as stale. This never runs under `--dry-run` (a dry run must not mutate anything); the duplicate check ignores such stale provisionals in both modes so dry-run and book agree on what is already booked, and `--list-bookings` / `--cancel-date` / `--cancel-booking` ignore them the same way (`is_active_booking`) — a leftover provisional is not a booking.

### 6.3 Timing between dates

2-second delay between processing each date to avoid hammering the API. No evidence of rate limiting at this interval over 1–3 month ranges. Skipped dates (§6.4) do not incur the delay.

### 6.4 Weekend and holiday skipping

Implemented in `dates.py` with no external dependency. `skip_reason(date, skip_weekends, skip_holidays)` returns `"weekend"`, the holiday name, or `None`.

Swedish red days are computed per year: fixed dates (Nyårsdagen, Trettondedag jul, Första maj, Nationaldagen, Juldagen, Annandag jul), Easter-derived (Långfredagen, Påskdagen, Annandag påsk, Kristi himmelsfärdsdag, Pingstdagen; Easter via the Meeus/Jones/Butcher algorithm) and window-based Saturdays (Midsommardagen = Saturday in June 20–26, Alla helgons dag = Saturday in Oct 31–Nov 6). The three eves — Midsommarafton, Julafton, Nyårsafton — are also included: they are not formal public holidays but are legally treated as Sundays and are de facto non-working days for commuters.

### 6.5 Partial booking (`book_partial`)

A round trip is always attempted the way the SJ app does it: one roundtrip search, one booking created from the outbound offer, return leg added to that booking via PATCH. Result: a single booking number for both legs.

- If the **return** leg has no 0-price offer (nor a close alternative), the outbound is kept and the booking is checked out outbound-only. This happens regardless of `book_partial`.
- If the **outbound** leg has no 0-price offer, nothing can be booked from the roundtrip search (the API requires an outbound offer to create a booking). With `book_partial = true` the tool then runs a one-way search for the return leg and books it as a separate booking; with `book_partial = false` the day is skipped.
- Missing legs are picked up on later runs via the duplicate check (§6.1 step 1), which searches only the missing direction as a one-way trip.

`handle_booking_process` returns `{"booking_id": ...}` when a booking was created and `None` when nothing was booked; `process_booking_flow` uses that to decide whether to run the fallback.

## 7. Departure Selection

### 7.1 Time matching

When `select_closest_ticket_available = true` (the expected default), walk the departures by absolute time difference from the target time, regardless of direction (earlier or later), and take the first one that carries the requested class (or a fallback class, §7.2): a closest departure without the class (a bus, a regional without calm) is skipped with a warning, not the end of the leg. If the chosen departure has no 0-price offer, one alternative is tried (§8.1).

When `false`, require an exact time match (the first exact match with the class). If there is no exact match, or the exact match has no 0-price offer, skip that leg — no other departure is ever booked; the run header says `exact time only`.

### 7.2 Class selection with fallback

When `allow_class_fallback = true` (default):

1. Try the requested `comfort_class`.
2. If unavailable, try `"2 class calm"` (unless that was the original request).
3. If unavailable, try `"2 class"` (unless that was the original request).

When a fallback occurs, **inform the user** which class was actually selected vs. what was requested.

The chain applies at **two levels**, both gated on `allow_class_fallback`: seat availability on the departure (serviceProperties) *and* the 0-price offer lookup (`find_offer_id`). A departure can carry seats in the requested class while the pass has no 0-price offer for it — e.g. `1 class` on an Årskort Silver, where the FIRST offer exists at a price > 0 — so the offer lookup falls through the chain on the same departure. Only when the chain is exhausted is the alternative departure tried (§8.1).

When `allow_class_fallback = false`, only match the exact requested class — at both levels. If unavailable, skip that departure.

### 7.3 Timezones

SJ API timestamps carry an explicit offset (`2026-09-01T06:59:00+02:00`). Rules, implemented once in `dates.py` (`parse_api_datetime`, `to_sweden`, `sweden_now`) and used everywhere:

- Train times, pass validity dates and config times are **Swedish wall-clock** times. API timestamps are converted to `Europe/Stockholm` before display or comparison with config values — never to the machine's local zone, so the tool behaves the same when run from abroad.
- "Is this in the past / expired / how many days left" comparisons use aware datetimes.
- A naive API timestamp (no offset) is assumed to be Swedish local time.

### 7.4 Station resolution

Station names are resolved to UIC codes via a hardcoded map in `SJClient`. This is sufficient for now. Future improvement: dynamic station lookup via the SJ API.

## 8. Edge Cases & Error Handling

### 8.1 No 0-price offer available

If no 0-price offer exists for any class in the fallback chain (§7.2) — every candidate is priced > 0, unavailable, or absent:
- With `select_closest_ticket_available = true`, try **one** alternative departure with the class: the closest one not later than the target for the outbound, not earlier than the target for the return (a second train at the exact target minute qualifies). With `false`, no alternative is tried.
- Log a warning with the date and direction.
- If this is the outbound leg, skip the entire date (no point booking a return without an outbound).
- If this is the return leg, keep the outbound booking and proceed to checkout as a one-way trip. Inform the user that only the outbound was booked.

### 8.2 Partial roundtrip

If the outbound books successfully but the return leg fails (no offer, sold out, or an API error while searching or adding it — caught and reported as `return leg failed (…), booking outbound only`):
- Keep the one-leg booking.
- Proceed to checkout with just the outbound.
- Inform the user: `no alternative found, booking outbound only`, `! no departure found for inbound, booking outbound only` or `! return leg failed (…), booking outbound only`; the day counts as `partly booked` in the summary.

### 8.3 Overlapping booking conflict

If the SJ API returns an error indicating an existing booking conflicts with the requested time:
- Print a warning with the date and conflict details.
- Skip to the next date.

### 8.4 Network failures & retries

Timeouts are explicit (`HTTP_TIMEOUT`: 30 s read/write/pool, 10 s connect) — the recorded booking calls take 3.5–4.3 s, httpx's 5 s default would cut them off on a slow day. The response body is read inside the retry loop, so a body that stalls or resets after the headers is a transport error of that attempt (retried for reads, raised as itself for writes), and a retried 502/503 releases its connection. Retry warnings never log an auth code. The token-refresh POST opts into the read policy (`RETRY_EXTENSION`). For transient HTTP errors (timeouts, 502, 503, connection errors, and transport errors such as a reset or a pooled connection closed by the server — `httpx.NetworkError` / `RemoteProtocolError`), in `RetryTransport`:
- Idempotent requests (GET/HEAD/OPTIONS): retry up to 3 times with delays of 1s, 2s, 4s (exponential backoff).
- Non-idempotent requests (POST/PATCH — provisional booking, add leg, checkout, cancel): retry only when the request provably never reached the server (connection error / connect timeout). A 502/503 or read timeout *after* sending is not retried — the server may already have acted on it, and a retry could create a duplicate provisional booking (which would only be cleaned up on the next `--book` run, §6.2).
- After the retries are exhausted, the current operation fails and is logged; the date loop continues with the next date.
- Do not retry on 4xx errors (these are not transient).

### 8.5 Unknown API response shapes

Every data endpoint goes through `_json_or_raise`: the API's own error envelope (`status` ≠ 200, `errorCode`/`error`, the SJ `code` + `message` + `validationErrors`) is raised as an `SJAPIError` — rendered `106 · Validation errors · outboundOfferId: OUTBOUND_OFFER_ID_MUST_BE_PROVIDED` — even behind an HTTP 200; a non-2xx without an envelope is an `HTTPStatusError`; an empty or non-JSON 2xx body is an `SJAPIError` too. An error never comes back as data. If the API returns a JSON response that doesn't match any known structure (no `status`, `errorCode`, or `error` keys, and no expected data keys):
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
- **Multiple valid passes**: for `--book`, the single pass whose validity covers the booking window is used without asking (a renewal bought ahead); otherwise a numbered list (`1. name (first day → last day)`) prompts for a choice — at a terminal only (`● 2 valid travel passes · run in a terminal to choose one`, exit 1, otherwise) and an empty answer ends the run (`● no travel pass selected`). The other pass-scoped modes need only a date range and take the longest-lived pass silently; `--list-travelpasses` lists every pass, expired ones included (`(expired)`), and never selects.
- **No valid passes**: exit with an error.

### 9.2 Bookable window validation

If the travel pass has validity dates (`startTravelValidityDateTime`, `endTravelValidityDateTime`), validate that the bookable window — first and last selected date from today on that the calendar filter does not skip — falls within the pass validity period (a week ending on a Sunday after a pass that ends on the Friday is fine; a selection with no bookable day skips the check), compared as **Swedish calendar dates**: the API's validity instants are midnight UTC (01:00/02:00 Swedish), and `endTravelValidityDateTime` is exclusive — the day after the last valid day, which is what `--list-travelpasses` shows as the end. Exit 1 with both ranges printed if not.

### 9.3 IDs

Two IDs are extracted from the travel pass:
- **Product ID** (`travelPassId`): used in search requests to indicate pass-holder pricing.
- **Passenger token**: used in booking creation. Resolved via `passengerToken` → `travelPassCreationBookingId` → fallback to product ID.

The search response may also return a `passengerListId` which takes precedence over the travel pass token for booking operations.

## 10. Output & Logging

### 10.1 Output channels

- **stdout**: user-facing status messages only, through `output.py` (one-space margin, no prefix). This is what the user sees at default log level.
- **stderr**: structured log output at all other levels.

### 10.2 User-facing output (stdout)

Everything the user sees goes through `output.py` and prints regardless of log level:

- `pinfo()` plain message, `pdim()` dimmed context, `spinner()` progress with a dim `✓`/`✗` trail line (or none with `trail=False`), `print_day_header()` / `print_day_note()` / `print_leg_lines()` for cards, `indented()` to nest everything printed inside a block under a day header.
- **Casing convention**: prose is lowercase (`no departure found for outbound`, `✓ checking offers…`), identifiers keep their case — booking numbers upper-case (`ERU0HWB2`), station names as SJ writes them (`Göteborg Central`), train names as given. Operation values (`booking tickets`, `listing bookings`) are lowercase; the header box's travelpass/holder values keep their real casing. Nothing is lowercased automatically any more; `pinfo` prints what it is given.
- Two output families share one vocabulary. **Pass-scoped modes** (book, dry-run, cancel, list-bookings, list-travelpasses): open with the header box (`print_header_box`, rounded dim borders): `operation` (bold: `booking tickets` / `dry run · booking tickets` / `cancelling bookings` / `listing bookings` / `listing travel passes`), `account` (the configured login email — always the second row, app-wide), `travelpass`, `holder` (account+owner only for travel passes — the passes are the content). Book/dry-run follow with the `describe_run` facts block (`route`/`days`/`times`/`ticket`, card grammar), then dim progress trail (routine fetches are silent: spinner only), cards, and a closing ● status line (`pstatus`: green done / red failed-or-aborted, dim text — cancel outcomes like `● booking X cancelled` / `● cancellation aborted` use the same line). **Auth modes** (login, logout, login-status): session-scoped and offline-capable, so no pass header — they open with a session-scoped header box instead (`operation` + `account`: config email for `--login`, cached profile_info email for `--logout`/`--login-status`), then any trail steps, then the status card (`print_status_card`: green/red dot + bold verdict, blank line, dim 7-char-padded labels with plain values, §5.6; `--login` ends by rendering the same card `--login-status` shows); travel passes render pass cards in the same fact grammar. No emoji anywhere in the output. Colour/bold/dim only on a TTY without `NO_COLOR`. Every printed line starts with a one-space left margin (`_MARGIN` in `output`) so output sits off the terminal edge; blank lines stay empty.
- Prompts accept `y`/`yes` for confirmation, any case. Every input prompt is inline (answer typed on the same line) and marked with a cyan `?` — the SMS prompt via `prompt()`, all interactive choices/confirmations via `ask()` (both in `output`). A `?` question that asks about a block above it (a card or numbered list) is separated from that block by a blank line; a prompt that is itself a trail step (the SMS code) stays attached to its trail. Trail lines use human step names (`✓ performing login`, `✓ sending sms code`, `✓ completing login` — not OAuth plumbing terms) with the mark coloured — green `✓` on success, red `✗` on failure — and the step text dim. Glyph colours form one quartet: cyan `?` input needed, green `✓` step succeeded, red `✗` step failed, yellow `!` deviation worth noticing (`pwarn`: class fallbacks, time deviations, alternative-departure attempts, rejected SMS codes, checkout failures, missing departures) — plus the green/red `●` on verdict cards and on every operation's closing status line (`pstatus`). Marks are coloured, message text stays dim. A blank line separates a login trail from the card or title that follows.

### 10.3 Log levels (stderr)

| Level | Content |
|---|---|
| CRITICAL | Default (silent). Only fatal unrecoverable errors. |
| ERROR | API errors, failed bookings, auth failures. |
| WARNING | Skipped dates, class fallbacks, unknown response shapes. |
| INFO | Per-date progress, selected departures, offer details. |
| DEBUG | HTTP request/response summaries, config values, token state. |
| TRACE | Full httpx/httpcore internals (request headers, response bodies). Cookie / Set-Cookie / Authorization / X-CSRF-TOKEN header values and `code=` query values are scrubbed from these lines too. |

`log_json()` redacts password/token values, the SMS `verification_code`, CSRF tokens and OAuth authorization `code` values (the API's numeric error `code` stays readable). An invalid `LOG_LEVEL` is reported on stderr directly (`invalid log level 'x' specified, defaulting to CRITICAL`) — the logger itself is about to be set to CRITICAL.

### 10.4 Log format

Structured key-value format on stderr:
```
time=2026-01-19T08:30:15.123+01:00 level=INFO     file=cli.py          class=SJTool              function=process_booking    msg="Selected outbound: 05:29 (diff: 0m)"
```

Color-coded by level in terminal.

## 11. Removed / Out of Scope

- **Ad-hoc trip booking**: different routes per date, multi-stop journeys.
- **Multi-passenger booking**: always single passenger (the pass holder).
- **Dynamic station lookup**: use hardcoded map for now.
- **SMS re-trigger**: user re-runs the tool if the SMS doesn't arrive (a mistyped code *is* retried, §3.2 step 8).
- **`dry_run` config key**: removed from config. `--dry-run` is a CLI modifier on `--book`/`--cancel-*`; the bare flags act for real.
- **Interactive confirmation before booking**: the config is the contract. The tool books what's configured.

## 12. Dependencies

- Python 3.13+
- `httpx` — HTTP client (the only runtime dependency)
- Declared in `pyproject.toml` (PEP 621) together with the `sj-tool` console script and the
  ruff/pytest/mypy configuration; install with `pip install -e .` (`--group dev` adds the dev tools)
- `pytest`, `ruff`, `mypy` (dev group only) — unit tests in `tests/`, no network
