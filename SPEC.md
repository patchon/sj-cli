# SPEC.md

Specification for the SJ API Client — a Python CLI tool that reverse-engineers sj.se to automatically book daily commute train tickets using an annual travel pass (e.g., SJ Årskort).

## 1. Purpose

Book train tickets for a daily commute over a date range (typically 1–3 months) using the 0-price benefit of an SJ travel pass. The tool authenticates via SJ's Azure AD B2C flow, searches for departures matching the user's time preferences, and books the best available option for each date.

This is **not** a general-purpose booking tool. It handles one route, one passenger (the pass holder), repeated across a date range.

## 2. Architecture

Standard src layout declared in `pyproject.toml` (PEP 621, `pip install -e .`). Modules organized by separation of concerns:

| Module | Role |
|---|---|
| `cli.py` | Entry point. CLI argument parsing (see §5.8), top-level orchestration. |
| `auth.py` | Authentication orchestration. B2C login flow, token lifecycle (validate → refresh → full login), SMS input with timeout. |
| `client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic. Includes HTTP retry logic. |
| `booking.py` | Booking business logic. The core every write path uses — `search()`, `resolve_offer()` (a departure → a Leg with the pass offer), `Cart` (provisional → add legs → seats → customer → checkout) — plus departure selection, offer matching, cancellation, duplicate detection, change-seat and upgrade-class modes, and the travel-pass validity helpers (`pass_validity`, `is_expired_pass`, `pass_covers`). |
| `config.py` | Configuration loading and validation (`CfgManager`). |
| `tokens.py` | Token cache management (`TokenManager`). Load, save, validate expiry, check refresh availability. |
| `logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. |
| `errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) and `error_text()`, the one-line, redacted rendering every printed exception goes through. |
| `output.py` | User-facing output helpers. `pinfo()`/`pdim()`, ANSI styling, spinner, per-day booking cards, status cards (auth modes), travel-pass cards. |
| `dates.py` | Swedish red-day calendar (Easter computed, no dependency). `skip_reason()` for weekend/holiday skipping. |
| `seats.py` | Seat selection: the config vocabulary, the seat-map join and the ranking. Pure logic — no HTTP, no printing. |
| `stations.py` | Station lookup for `--book-journey`: folds and ranks SJ's public station list (exact › prefix › word prefix › substring › synonym, ties by UIC code). Pure — no HTTP, no printing. |
| `journey.py` | The interactive journey mode: the questions, the pick lists, one confirmation, then the `Cart`. |

### Design principles

- **Single responsibility**: each module has one job. The HTTP client doesn't decide what to book. The booking logic doesn't manage tokens.
- **No business logic in the client**: `client.py` exposes methods like `search_journey()`, `get_offers()`, `create_provisional_booking()`. It does not interpret results or make decisions.
- **Thin entry point**: `cli.py` parses CLI args, wires modules together, and delegates. The main loop body calls into `booking.py`.
- **Errors as exceptions**: use typed exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`) rather than return-code checking. Catch at the orchestration layer.

### Data flow

```
CLI args (see §5.8)
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
  --change-seat-date DATES    → find bookings on route → apply seat_preference → exit
  --change-seat-booking NUMS  → find bookings by number → apply seat_preference → exit
  --book-journey          → questions (date/from/to/return) → search → pick a departure per leg → confirm → Cart → exit
  --login                 → exit after auth
  --logout                → end B2C session, delete caches → exit (skips config/auth)
  (--dry-run modifies --book / --book-journey / --cancel-* / --change-seat-* / --upgrade-class: preview only, nothing mutated)
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

1. **Load cached token** from `~/.cache/sj-cli/token.json`.
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

`~/.config/sj-cli/config.toml` (or `$XDG_CONFIG_HOME/sj-cli/config.toml`)

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
seat_preference = ["window", "table", "forward"]  # optional; or "ask" to be prompted (§4.3)
```

### 4.3 Validation rules

Validation runs at startup before any API calls, scoped to the operation: `[auth]` is always validated; `[search_parameters]` only for the operations that use the route (`--book`, `--cancel-date`, `--change-seat-date`) — login, listing and `--cancel-booking` work with a config holding only credentials — and the `dates` selection only for `--book`: `--cancel-date` and `--change-seat-date` take their own dates (from the CLI), but still validate the rest of `[search_parameters]` (`time_leave`, `roundtrip`/`time_return`, the stations, `comfort_class`, `flexibility`, `select_closest_ticket_available`). `seat_preference` sits outside that scoping: it is validated whenever `[search_parameters]` is present, in every mode, and is additionally required — its absence is itself an error — for `--change-seat-date` and `--change-seat-booking` (§5.4); `--change-seat-booking` therefore does *not* work with a credentials-only config, even though it skips the rest of `[search_parameters]`. Fail fast: the errors render as a status card — `● invalid configuration` (red dot + bold verdict), a blank line, then one plain indented line per error — and the run exits 1. `SJConfigError` carries the individual messages as `.errors`; file-level failures (missing/unparsable config) render the same card with a single line.

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
| `seat_preference` | Optional (absent means SJ assigns the seat); required for `--change-seat-date` / `--change-seat-booking` (§5.4). Either the literal `"ask"` (prompt for every leg) or a list of ranked vocabulary words: `window`, `aisle`, `table`, `solo`, `single`, `easy access`, `no animals`, `forward`, `backward` — an earlier word in the list outweighs every later one combined. Any word may also be negated as `avoid <word>` (`avoid table`), which is met by exactly the seats the plain word is not; the ranking is unchanged (lexicographic), so the position sets the strength — `["avoid table", "single", "aisle", "window", "forward"]` puts every table-free seat ahead of every table seat, then singles, then aisle-forward, aisle-backward, window-forward, window-backward. A negation is best-effort like every other wish: when only avoided seats are free, one is taken anyway (§5.4). `solo` is SJ's first-class "Singelplats" product (a marketed property code); `single` is any seat with no neighbour, computed from the carriage's 2+1 layout geometry rather than read from a code — the two are not the same and a seat can be either, both or neither. Words are normalised in place (lower-cased, inner whitespace collapsed to one space). Rejected: a value that is neither `"ask"` nor a list of strings (`seat_preference must be "ask" or a list of: aisle, backward, easy access, forward, no animals, single, solo, table, window — each also as "avoid <word>"`); an empty list (`seat_preference is empty — omit the key to let SJ assign the seat`); an unknown word, including an `avoid` naming one (`seat_preference: unknown "middle". Valid words: …`, `seat_preference: unknown "avoid middle". Valid words: …` — the listing ends `— each also as "avoid <word>"`); a wish listed twice, negated or not (`seat_preference lists window twice`); `forward`+`backward` together (`seat_preference cannot ask for both forward and backward`) — every seat is one or the other, so naming both can never change the order and is a typo rather than a wish; and any word together with its own negation (`seat_preference cannot ask for both table and avoid table`). `window`+`aisle` is **allowed**, and so is `single`+`aisle`: a seat can be neither, and an earlier word outranks a later one, so `["aisle", "window"]` is a fallback order — aisle seats first, window seats next, the rest last — not a contradiction. Nothing in a list is a guarantee anyway: the best remaining seat is taken whatever it satisfies. |

Report all validation errors at once (don't stop at the first one).

### 4.4 Token cache

`~/.cache/sj-cli/token.json` — auto-created on first successful login. Contains `access_token`, `refresh_token`, `expires_on`, etc. SSO cookies for silent re-login are cached next to it in `cookies.json`. Both are deleted by `--logout`.

## 5. CLI Interface

### 5.1 Book mode

```bash
sj-cli --book
```

Reads config, authenticates, and books tickets for every selected date (`dates`, §4.3). No confirmation prompt — the config is the source of truth. Output opens with the **header box** (`print_header_box`): a rounded dim-bordered box holding dim-labelled rows — `operation` (bold value, `booking tickets`; dry run prefixes `dry run · `), `account` (config email), `travelpass` and `holder` (real casing) — then a blank line and the run's config as dim-labelled facts in the shared card grammar: `route`, `days` (span + day filter, e.g. `weekdays only` — a contiguous selection renders as `1 sep – 30 oct 2026`, anything else as `W43, W45..46 (19 oct – 15 nov 2026)`), `times`, and `ticket` (class, flexibility, train filter, and any non-default switches such as `exact time only`, `no class fallback`, `partial ok`). Then one **day card** per date (the same card shape as `--list-bookings`, §5.5): a bold date + route header, the progress trail and any messages indented beneath it, then the booked legs, and a blank line. Days that need no work are a single line (bold date + dim reason). The run closes with a status line (`pstatus`): ● coloured by outcome — green when the run booked something, dim when it changed nothing (a dry run, or every day already booked or skipped), red when a day failed or errored — and dim summary text.

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
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 class calm   FULLFLEX   ERU0HWB2
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 66   2 class calm   FULLFLEX   ERU0HWB2

wed 16 sep 2026   tickets already booked

sat 19 sep 2026   weekend

thu 24 sep 2026   Göteborg Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  no departure found for outbound
  nothing booked

● 4 day(s) · 1 booked · 1 already booked · 1 not booked · 2 skipped
```

The booked legs are rendered from the booking object the API returns (train, carriage/seat, class, flexibility, booking number), so they are identical to what `--list-bookings` shows afterwards. A checkout failure prints `checkout failed, provisional left (cleaned up on next --book run)` and counts as `checkout failed` in the footer; an exception while processing a day prints `error: …` and counts as `error(s)`. Either turns the closing `●` red and makes the run exit 1 (§5.9); days without a 0-price offer are skips (`not booked`), not failures. The route in the header shows what the day needs: `A ⇄ B` both legs, `A → B` outbound only, `B → A` return only (with a `return already booked, searching outbound only` / `outbound already booked, searching return only` line beneath).

### 5.2 Dry-run mode

```bash
sj-cli --book --dry-run
```

`--dry-run` is a **modifier**, not an operation: it composes with `--book`, `--cancel-date`, `--cancel-booking`, `--change-seat-date`, `--change-seat-booking` and `--upgrade-class`, and means nothing real happens. Bare `--dry-run`, or `--dry-run` with any other flag, is a usage error. With `--book` it performs the full search flow for each date but does **not** create bookings (and skips the stale-provisional cleanup). Same run header and day cards as §5.1, with the operation prefixed `dry run · `; each leg line shows the departure that would be booked — time range, duration, train, class, flexibility — or, dimmed in place of the flexibility cell, why it cannot be booked (`no 0-price offer`, `no departure found`). The footer starts with `dry run` and counts bookable / partly bookable / unavailable days.

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
sj-cli --cancel-date 2026-01-20              # all bookings on the configured route that day
sj-cli --cancel-date 2026-01-20,2026-02-03..2026-02-05   # several dates, comma list + ranges
sj-cli --cancel-date W43                      # a whole ISO week
sj-cli --cancel-booking ERU0HWB2,8Y41N08J   # by booking number, any case
```

For each matching booking: show its day card (same shape as §5.5, no title), then confirm. `y`/`yes` (any case) confirms; anything else aborts. `--cancel-date` cancels only that day's journeys: a booking made on sj.se may hold journeys on other days, which are neither shown nor touched — `1 other journey in booking X on other dates is kept` under the card, the prompt reads `cancel this journey from booking X?`, and the status line `1 of 2 journey(s) cancelled from booking X`.

```
╭────────────────────────────────────╮
│  operation    cancelling bookings  │
│  account      user@example.com     │
│  travelpass   SJ Årskort Silver    │
│  holder       John Doe             │
╰────────────────────────────────────╯

✓ searching for booking ERU0HWB2

tue 15 sep 2026   Göteborg Central → Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 class calm   FULLFLEX   ERU0HWB2

? cancel booking ERU0HWB2? [y/n]: y
✓ cancelling booking ERU0HWB2

● booking ERU0HWB2 cancelled
```

If the booking has several future journeys, they are listed as numbered leg lines (`1.`, `2.`, … and `a.` for all); the selected legs are echoed as leg lines under `selected for cancellation:` before the final `? cancel selected journey(s)? [y/n]:`. All cancel prompts (`? select [1/2/a]:`, the `[y/n]` confirmations) use the shared inline `?`-marked prompt (`ask()` in `output`). Past journeys are shown but cannot be selected. A booking with a pending cancellation offers confirm / revert / nothing. With `--dry-run`, the day cards are shown and the run stops there — no prompts, no cancellation calls — closing with `● dry run · N journey(s) would be cancelled from booking X` per booking (or `● dry run · booking X has a pending cancellation, nothing done`).

### 5.4 Change-seat mode

```bash
sj-cli --change-seat-date 2026-09-16                 # re-seat that day's journeys on the configured route
sj-cli --change-seat-date W43 --dry-run              # preview which seats it would take, nothing written
sj-cli --change-seat-booking ERU0HWB2,8Y41N08J       # re-seat booking(s) by number, any route
```

Re-seats already-confirmed bookings using `seat_preference` (§4.3), which both flags require. `--change-seat-date` matches the same way `--cancel-date` does — active bookings with a journey on the configured route (either direction) on that Swedish date — and touches only that day's segments; a booking's other days are left alone. `--change-seat-booking` takes booking numbers directly (same grammar as `--cancel-booking`: comma-separated, any case) and re-seats every segment of the booking, whatever route it runs; an unknown number is reported (`● no active booking found with number X`) and the run continues with the rest. Unlike `--cancel-date`'s one-section-per-date output, both flags process everything they were given in a single pass and close with one status line.

A segment that has already departed is skipped before any seat map is read (`already departed, skipped`); one whose seat map itself reports `hasDeparted` or `canChangeSeat: false` is left alone the same way (`seat cannot be changed`) — neither counts as a failure. Otherwise seats are chosen exactly as during `--book` (§6.1 step 10): the seat map is read via `GET /bookings/{id}/seatmap/{seatMapSearchId}`, the best free seat — or, in `"ask"` mode, the interactively picked one — is written through `PATCH /bookings/{id}/seats`, the confirmed-booking endpoint (no confirm step, unlike cancellation). `--dry-run` reads the same seat maps and reports the choice without writing anything: `would take carriage 3 seat 34 · window, forward` per leg in word-list mode, or the current seat plus how many are free in `"ask"` mode, which never prompts under `--dry-run` (matching §5.2).

```
╭──────────────────────────────────╮
│  operation    changing seats     │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       John Doe           │
╰──────────────────────────────────╯

tue 15 sep 2026   Göteborg Central ⇄ Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 34   2 class calm   FULLFLEX   ERU0HWB2
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 7 seat 12   2 class calm   FULLFLEX   ERU0HWB2

● 2 seat(s) changed
```

The closing status is green only for a real run that actually wrote a seat (`N seat(s) changed`); a real run that matched something but changed nothing — every segment already held its best seat, or none were changeable — closes dim (`nothing changed`), and so does every dry run that matched something (`dry run · nothing to change`): a dry run never turns the line green, even when its per-leg lines say `would take …`, because nothing is actually written. Nothing matching at all closes red (`no bookings matched`). As everywhere in this feature (§10.2): a seat preference is never worth failing a booking over — a map that will not load, no free seat to choose from, or a rejected PATCH each degrade to keeping the seat SJ assigned and print a `!` line rather than aborting; the exit code is affected only by an unresolved `--change-seat-booking` number or an unexpected failure while applying a change (§5.9).

### 5.5 List current bookings

```bash
sj-cli --list-bookings
```

Fetches all active bookings within the travel pass validity period and displays them as one card per travel day, legs indented beneath:

```
tue 18 aug 2026   Göteborg Central ⇄ Stockholm Central   past
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 34   2 class calm   FULLFLEX   ZR8C6RT1
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 22   2 class calm   FULLFLEX   ZR8C6RT1

wed 19 aug 2026   Göteborg Central ⇄ Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 30   2 class calm   FULLFLEX   TBRS43MG
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 55   2 class calm   FULLFLEX   TBRS43MG

mon 31 aug 2026   Göteborg Central ⇄ Stockholm Central
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 7 seat 32   2 class        FULLFLEX   3RK7YJU4
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 21   2 class calm   FULLFLEX   K883DH2T

● 3 day(s) · 4 booking(s) · 2 in the past
```

- Header per day: date, route (`A ⇄ B` when both directions are present, otherwise the distinct routes), and a `past` tag when every leg has departed.
- Leg line: direction arrow, departure – arrival, duration, train (brand + number), carriage/seat, class, flexibility, booking number. The arrow is inferred from the route (reverse of the day's first leg → `←`) because the API reports a standalone return booking as `OUTBOUND`.
- Grouped by date rather than booking number: a return leg booked via the `book_partial` fallback (§6.5) is its own booking, so the booking number is shown per leg. Legs are sorted by departure time.
- A day whose legs have all departed is dimmed; when colour is unavailable a `past` tag is appended instead.
- Only shows non-cancelled bookings. Pagination: fetches all pages from the bookings API. Read-only.
- Opens with the header box (`operation   listing bookings` + account/travelpass/holder), then the day cards; the bookings fetch is a silent spinner. The closing status line (dim ●, dim text — a listing changes nothing) counts days and bookings (leg count omitted — the legs are visible in the cards), plus `N in the past` when applicable. Elsewhere, progress steps that matter leave a dimmed trail line when done — `✓ searching outbound at 06:59`, or `✗ …` if the step raised — so logs show where time went or where a step failed. When stdout is not a TTY only the trail line is printed.
- Styling: bold header/booking numbers, dimmed past days, coloured arrows. ANSI colour is emitted only when stdout is a TTY and `NO_COLOR` is unset (`TERM=dumb` also disables it); piped output is plain text. Emoji appear only in run-mode title lines, never in footers or aligned rows.
- The card renderer (`day_header`, `leg_lines` in `output.py`) is shared by list, book, dry-run and cancel output; columns that are empty on every row of a card are omitted, so book/list cards show train · seat · class · flexibility · number while dry-run cards show train · class · flexibility/note. Travel passes render as cards in the same grammar (`print_travelpasses`): bold pass name + card number as the header, then dim-labelled facts (holder, valid with days left, price from the receipt); unknown facts are omitted, and a `● N travel pass(es)` status line closes the mode.

**`--seat-details`** (modifier, `--list-bookings` only, §5.8): appends the assigned seat's characteristics to the seat cell, e.g. `carriage 3 seat 34 · single, window, table, forward`, in the same vocabulary `seat_preference` uses (§4.3) plus `forward`/`backward` (the API's `IDR`/`ODR` codes). The bookings-list endpoint's own `seatProperties` are always empty, so this reads them from that leg's seat map instead — one extra `get_seatmap` request per leg, hence opt-in. The assigned seat is first looked up by carriage and seat number in that map's carriage layout, the same join `free_seats()` does, so `single` can be computed from the layout's geometry; when the seat cannot be found there (an unfamiliar map shape), rendering falls back to the assigned entry's own `carriageSeatProperties` codes alone — that path can never report `single`, since no code carries it. Only fetched for a leg that has not yet departed and carries `seatMapAvailable`/`seatMapSearchId`; a map fetched for one leg is reused if another leg's `seatMapSearchId` matches it. A map that will not load, has no assigned seat, or carries no recognisable property code leaves that leg's seat cell as the plain `carriage N seat M` and is counted; the run closes with one aggregated `! seat details unavailable for N leg(s)` rather than one warning per leg.

When `seat_preference` (§4.3) is a ranked word list, a leg whose seat map holds a strictly better free seat gets a further `· could take <n> · <words>` appended, naming that seat's number and its own words in the same vocabulary — e.g. `carriage 3 seat 21 · aisle, backward · could take 47 · single, window, forward`, so it is obvious at a glance which tickets are worth re-seating (`--change-seat-date`/`--change-seat-booking`, §5.4, apply the change). "Better" is judged by the exact ranking `best_seat` uses to choose a seat (§4.3, §5.4) — never by whether the two seats merely differ, since `best_seat` is best-effort and can return the lowest-numbered free seat even when it satisfies no wish at all. The hint needs a wish list to judge "better" against, so it stays silent whenever `seat_preference` is `"ask"` or absent (`[search_parameters]` itself may be entirely absent from a config valid for `--list-bookings`, which does not require it) — and, like the plain seat cell, silent for a leg already on its best seat, one with nothing free, one already departed, or a map that will not load:

```
 fri 28 aug 2026   Linköping Central ⇄ Stockholm Central
   → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 34 · window, table, forward   2 class calm   FULLFLEX   EPPE0XKQ
   ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 19 · aisle, backward · could take 47 · single, window, forward   2 class calm   FULLFLEX   EPPE0XKQ
```

### 5.6 Upgrade-class mode

```bash
sj-cli --upgrade-class 2026-09-16 --dry-run   # report which legs could be upgraded, touching nothing
sj-cli --upgrade-class W40                    # release and re-book those legs, after one confirmation
sj-cli --upgrade-class 2026-09-16,2026-09-21..2026-09-25   # same date grammar as --cancel-date
```

Moves legs already booked on the configured route out of a fallback comfort class and into `comfort_class` (§4.3). Legs are matched the way `--cancel-date` matches them — active bookings with a journey on the configured route (either direction) on that Swedish date, not yet departed — and a booking's other days are never looked at. A leg already in `comfort_class` needs nothing, prints no card and is only counted.

**What the probe can and cannot prove.** SJ has no change-class operation, and the pass cannot hold two overlapping tickets: a journey search made *with* the travel pass reports every class `unavailable` for any departure the account already holds (the sj.se UI calls this *"Samtidigt som annan bokning"*) and gives no reason for it — `unavailableReasons` is `[]` whether the departure is free or blocked. So booking before cancelling is impossible, and the only search that tells the truth about a held departure is one made **without** a travel pass id. That search proves whether SJ *sells* a seat in the wanted class, at any price. It can never prove the pass would claim one for free — pass quota is a separate pool — so the report says `no seats on this departure` with certainty and `seats exist (SJ sells them) — an upgrade may be possible` as a maybe, never a promise.

**The consequence, stated plainly:** an upgrade is a release followed by a purchase, with no way to keep the old ticket as a safety net. If the pass gets no offer after the release, the run falls down the ordinary class chain (§7.2), and if that fails too the leg ends **with no ticket at all**. Passing the flag without `--dry-run` is the consent for that risk; the tool never takes it on its own.

Phases:

1. **Probe** (read-only, both modes). For every future leg in a fallback class, the same route/date is searched without the pass, the exact departure the booking holds is re-found (on departure minute + train identifier, never "closest to `time_leave`" — that would report, and later book, a different train), and its offers are read. `--dry-run` stops here and closes with `● dry run · N leg(s) not in <class> · M worth trying`.
2. **Gate.** Only legs the probe answered *yes* for are candidates. A leg with no seats, an unknown answer (the departure could not be identified), or no `serviceIdentifier` to release is reported and left alone — the held ticket is the best available, and releasing it to find that out is never right.
3. **Confirmation.** One question for the whole run, after listing every leg it will attempt (date, train, booking number, current class) and the warning `! each ticket is cancelled before the new one is searched · if the pass gets no offer after that, the leg ends with no ticket`. The prompt is `? upgrade N leg(s) to <class>, cancelling each ticket first? [y/n]: `; `y`/`yes` (any case) proceeds, anything else aborts with `● upgrade aborted, nothing was cancelled` and writes nothing.
4. **Per leg, in one uninterrupted step.** `PATCH /bookings/{id}` cancels **exactly one** `serviceIdentifier` — the journey being upgraded, so a roundtrip booked as one booking keeps its other journey — followed by the cancellation confirm, then immediately the same departure is searched **with** the pass, matched again by departure minute + train, and booked through the ordinary sequence (provisional → seats via `seat_preference` → customer PATCH → checkout, §6.1 steps 8–11). Nothing else happens between a cancel and its own re-book, so the window in which nothing is held is one search plus one booking call wide.

Per-leg outcomes:

| Report | Meaning | Exit |
|---|---|---|
| `upgraded to <class> · new booking N`, then the new leg line | The wanted class was booked; the leg line shows the train, seat and new booking number. | 0 |
| `! no gain: re-booked in <class> again · new booking N` | The pass had no offer in the wanted class after the release; the class chain landed back on the class the leg started in. A ticket exists, so this is not a failure. | 0 |
| `! fell back to <class> · new booking N` | As above, but into a third class (neither the wanted one nor the one it held). | 0 |
| `! no ticket for this leg: the old one is cancelled and nothing was booked back` + `recover: …` | The worst outcome: the ticket is gone and nothing replaced it. | 1 |
| `! could not release the ticket: …` + `the ticket is untouched, so nothing was booked either` | The cancel PATCH failed; no booking is attempted after a failed release. | 1 |
| `! cancellation started but not confirmed: …` + `resolve it with: sj-cli --cancel-booking N` | The PATCH landed but the confirm did not, leaving a pending cancellation only the user can resolve or revert (§5.3); nothing is booked on top of it. | 1 |

The `recover:` line names the exact way back for that leg: `run: sj-cli --book` when the date is inside the config's `dates` selection **and** `--book` would not skip it as a weekend or red day, otherwise `book it again on sj.se by hand` with the reason (outside the selection, or the skip reason). Every leg that ended without a ticket is listed once more, with its recovery line, immediately before the closing status — that outcome must not be something a scrollback can hide.

Guards, none of them optional:

- **Never without a pass to re-book on.** Without `--dry-run`, a run that resolved no travel pass product is refused before anything is touched (`● no travel pass to re-book with · nothing was touched`): the re-book would search as an anonymous customer, find no 0-price offer, and the release could only lose the ticket.
- **Never unattended.** Without `--dry-run`, a stdin that is not a terminal is refused before a single booking request: `! upgrading cancels each ticket before re-booking it, so it needs a terminal to ask` + `● not a terminal · use --dry-run to see what it would attempt`, exit 1. A cron job cannot release tickets.
- **Never without the probe.** A leg the probe rejected is never cancelled.
- **Never wider than asked.** Only the given dates, only the configured route, only the one journey per cancel payload.
- **Never a cancel without its re-book.** The two are one step; an exception in the re-book still ends in a report, never in an unwound run that says nothing about the leg.

```
╭──────────────────────────────────╮
│  operation    upgrading class    │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       John Doe           │
╰──────────────────────────────────╯

tue 15 sep 2026   Göteborg Central → Stockholm Central
  X 2000 520 · ERU0HWB2 · holds 2 class
    2 class calm: seats exist (SJ sells them) — an upgrade may be possible

1 leg(s) to upgrade to 2 class calm:
  tue 15 sep 2026   X 2000 520 · ERU0HWB2 · holds 2 class

! each ticket is cancelled before the new one is searched · if the pass gets no offer after that, the leg ends with no ticket
? upgrade 1 leg(s) to 2 class calm, cancelling each ticket first? [y/n]: y

tue 15 sep 2026   Göteborg Central → Stockholm Central
  X 2000 520 · ERU0HWB2 · holds 2 class
    ✓ releasing this journey from booking ERU0HWB2
    ✓ searching the same departure with the travel pass
    ✓ checking offers for the same departure at 06:59
    ✓ booking 2 class calm
    ✓ checking out booking ZSVV7EML
    upgraded to 2 class calm · new booking ZSVV7EML
    → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 34   2 class calm   FULLFLEX   ZSVV7EML

● 1 leg(s) attempted · 1 upgraded to 2 class calm
```

The closing status is green when a ticket was actually bought, red when any leg was harmed (no ticket, an unconfirmed cancellation, a failed release) or the run was aborted, dim when it ran clean and bought nothing. Nothing matching the dates at all closes red (`● no bookings found for the given dates on route A → B`) but is not a failure.

### 5.7 Environment variables

| Variable | Effect |
|---|---|
| `LOG_LEVEL` | Set log verbosity: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default: `CRITICAL` (silent). |
| `NO_COLOR` | If set (any value), disable ANSI colour/bold in output. Colour is also disabled automatically when stdout is not a TTY. |

### 5.8 CLI flag summary

| Flag | Description | Interactive? |
|---|---|---|
| `--dry-run` | Modifier for `--book`/`--book-journey`/`--cancel-date`/`--cancel-booking`/`--change-seat-date`/`--change-seat-booking`/`--upgrade-class`: preview only, nothing booked, cancelled, re-seated or upgraded, no prompts. Usage error alone or with any other flag. | Only SMS on first login. |
| `--book` | Book tickets for the configured dates (`dates`, §4.3). | Only SMS on first login; also prompts per leg when `seat_preference = "ask"` (not under `--dry-run`). |
| `--book-journey` | Book one journey interactively (§5.10): date, from, to and a return are asked (config values are the defaults), each leg's departures are listed to pick from with the class the pass would get, one `book?` confirmation, then the booking. Terminal only, dry run included. | Yes (every prompt; also per leg when `seat_preference = "ask"`). |
| `--cancel-date DATES` | Cancel bookings on the configured route for one or more dates: a `YYYY-MM-DD` date, an ISO week (`W43`, `2027-W02`; a bare week is this ISO year), a comma-separated list, and/or inclusive `START..END` ranges, mixed freely. Every token is validated up front (the same grammar as the config's `dates` key, §4.3); any problem renders an `● invalid --cancel-date` card echoing each bad value and exits 1 before any API call. Dates are deduplicated and processed in order, one section per date. | Yes (journey choice + confirmation). |
| `--cancel-booking NUM[,NUM…]` | Cancel booking(s) by booking number, comma-separated, any case (deduplicated, order kept). Validated up front: a value that cannot be a booking number (anything beyond letters and digits) or that looks like a date or week (`2026-09-16`, `W43`, `2027-W02`, a `..` range) renders an `● invalid --cancel-booking` card echoing each bad value — with a `did you mean --cancel-date?` hint for the date/week-shaped ones — and exits 1 before any API call. One output section per booking, blank-line separated. | Yes (journey choice + confirmation). |
| `--change-seat-date DATES` | Re-seat bookings on the configured route for one or more dates, using `seat_preference` (required, §4.3): same date grammar and up-front validation as `--cancel-date` (§5.4). All given dates are processed in one pass, closing with a single status line. | Only SMS on first login, unless `seat_preference = "ask"` (prompts per leg per day, not under `--dry-run`). |
| `--change-seat-booking NUM[,NUM…]` | Re-seat booking(s) by number, comma-separated, any case — same validation as `--cancel-booking` — using `seat_preference` (required, §4.3). No route filter: a number is already an explicit target (§5.4). | Only SMS on first login, unless `seat_preference = "ask"` (prompts per leg, not under `--dry-run`). |
| `--upgrade-class DATES` | Move legs on the configured route out of a fallback comfort class into `comfort_class` (§5.6): same date grammar and up-front validation as `--cancel-date`. Each leg's ticket is **cancelled and then re-booked** — SJ has no change-class operation and the pass cannot hold two overlapping tickets — so a leg can end up with no ticket at all; passing the flag without `--dry-run` is the consent for that. Asks once, listing every leg it will attempt, before writing anything, and refuses outright when stdin is not a terminal. `--dry-run` reports what it would attempt and touches nothing. | Yes (one confirmation; also per leg when `seat_preference = "ask"`). |
| `--list-bookings` | Display all active bookings as per-day cards. | Only SMS on first login. |
| `--seat-details` | Modifier for `--list-bookings` only (§5.5): append each not-yet-departed leg's assigned seat characteristics (window/aisle/table/solo/single/forward/backward) to its seat cell — one extra `get_seatmap` request per eligible leg. When `seat_preference` is a ranked word list, also names a strictly better free seat when one exists (`· could take <n> · <words>`); silent under `"ask"` or an absent preference. Usage error alone or with any flag other than `--list-bookings`. | Only SMS on first login. |
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

### 5.9 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Any failure: config or auth error; a `--book` run in which any day's checkout failed or errored; a `--cancel-*` run in which any cancellation was refused by the API, declined at a prompt, or (`--cancel-booking`) the number was not found; a `--change-seat-*` run in which (`--change-seat-booking`) a named number was not found; an `--upgrade-class` run in which any leg ended with no ticket, any release failed or was left unconfirmed, a probe raised, the confirmation was declined, or there was no terminal to ask at (§5.6); a `--book-journey` run that was refused (no terminal), aborted at a prompt, declined at `book?`, found no departures, could not fetch the station list, could not create the booking, or whose checkout failed. A day with no offer, a `--cancel-date` date with nothing to cancel, a segment for which `--change-seat-*` found no free seat to choose from or whose write was rejected (kept as SJ assigned, reported with `!`, §5.4), and an `--upgrade-class` leg that was re-booked into a lower class than asked for (a ticket exists) are not failures. |

Every failure that ends a run closes with a red `●` status line naming the cause (`● initialization failed: …`, `● error: …`, `● token refresh failed: …`, `● no valid travel pass found`). Error texts are one line (httpx's "for more information" line is dropped), never empty (an exception without a message shows its type) and never carry an auth code from a URL.

### 5.10 Book-journey mode

```bash
sj-cli --book-journey             # ask, pick, confirm, book
sj-cli --book-journey --dry-run   # the same questions and lists, nothing written
```

One journey, booked the way sj.se's front page does it, on the travel pass. Everything not typed comes from config, so Enter at every prompt books one day of the configured commute:

```
? date [2026-08-29]: 2026-09-14      today by default (the pass start when that is later);
                                     never before today, always inside the pass validity
? from [Göteborg Central]:           Enter keeps the config station; typing filters the live
? to [Stockholm Central]: upps       station list as you type (↑↓ move, Enter picks, Esc aborts)
? return? [Y/n]:                     the default is `roundtrip`
? return date [2026-09-14]:          Enter = the same day; never before the outbound date
```

Stations come from SJ's public `config/stations` list, matched case- and diacritic-insensitively: an exact name or synonym wins, then a name prefix, a word prefix, a substring, a synonym substring — ties by UIC code, which puts the big stations first. When any match is a train-looking station (a station word such as `Central`/`station`/`Resecentrum`/`Flygplats` in the name, or a non-Swedish UIC code whose last five digits fall below the `9xxxx` range the bus terminals live in), matches ranked no better than it are dropped unless they are train-looking too — `linköping` gives `Linköping Central` alone, while a stop typed by its exact name still comes first; a bus stop is always found when nothing train-looking matches. The 6-station map in `client.py` is still what config validation uses (offline); the picked codes go straight into the search. A `to` equal to `from` is refused (`! from and to are the same station`) and asked again, without the default.

One search (with the return date when asked), then per leg a day header and a pick list — `HH:MM → HH:MM   duration   train   class`, the class being what the pass would get on that departure (`comfort_class`, a fallback marked `fallback`, or `—` marked `no seats`, which cannot be picked) — that column is what the search reports, and the offer step can still fall further down the class chain (§7.2), said as `! outbound class fallback: 2 class calm → 2 class`. Departures already gone are dropped before the lists are built and counted under the day header (`2 already departed`) — the clock is read after the questions, so a train that left while they were being answered is gone from the list too; a leg with nothing left ends the run (`● no departures left for Göteborg Central → Stockholm Central today` when they have all gone, `● no departures found for Göteborg Central → Stockholm Central …` when the search itself came back empty — `today` or `on 2026-09-14` by the leg's date), exit 1. The highlighted row is the enabled departure closest to `time_leave` (`time_return` for the return — 17:00 when the config omits it, which it may when `roundtrip = false`); ↑↓ (Tab/Shift-Tab) or a typed row number move it, Enter picks, Esc aborts. A row that cannot be picked is never highlighted: it is drawn dim, the arrows pass over it (wrapping), a typed number pointing at one shows its reason in the footer and leaves the highlight where it was, and a default that lands on one starts on the next selectable row instead. When every row is refused the list still opens — with the highlighted row's reason in the footer, the hint line's `· Esc aborts` appended to it, so the refusal is visible and the way out is still on screen. The pick's offers are read at once; a departure without a 0-price offer is said (`! no 0-price offer at 05:29 · pick another`), disabled, and the list asked again with the closest enabled row highlighted — pick after pick that can leave every row refused, and Esc is the way out of that list as of any other. Before the first list, every ticket the account already holds on the chosen dates is named (`! you hold booking 3HT2NEIL on 2026-09-14 (Göteborg Central → Stockholm Central 05:29) · departures overlapping it are not selectable`). Two notes follow from that on the rows themselves: a row that *is* a held train — a held segment leaving at the same instant on the same train (brand + number) or the same route — reads `already booked in 3HT2NEIL` and is refused as `this journey is already booked in 3HT2NEIL · pick another`; only when no held segment is that train does a plain time intersection apply, reading `overlaps 3HT2NEIL · Göteborg Central → Stockholm Central 04:01–08:38` and refused as `overlaps booking 3HT2NEIL · pick another` (the held ticket's own route is always named in front of its times, this list's route included, and a night train that departed the evening before counts as well, though only tickets on the chosen dates get the `! you hold` line). Both notes stand in place of the `fallback`/`no seats` one, and **both rows are refused whatever class the search reports** — the class column keeps what the search said. A pass search usually reports every class unavailable on a departure overlapping a held ticket (§5.6), but not always: SJ itself does not refuse a pure time overlap (it has sold a ticket three minutes into another one), so refusing both kinds here is this tool's rule, not SJ's. When SJ's own offers response names a double booking the pick prints `! outbound: SJ reports a conflict with booking 3HT2NEIL` and stands — the line comes from `resolve_offer`, so `--book` prints it too. A failed bookings fetch is only a note (`! could not check existing bookings: …`). There is no duplicate check — that warning is the whole guard; a day `--book` already covered can be booked again.

Then the chosen leg(s) as day cards (one per date), and:

- `--dry-run`: `● dry run · nothing booked` (dim), exit 0.
- `? book? [y/N]:` — no: `● booking aborted, nothing was booked` (red), exit 1.
- yes: the ordinary write path (§6.1 steps 8–12 through the `Cart`: provisional, the return leg added to it, seats when `seat_preference` is set, customer, checkout), the booked card (class and flexibility from the segment's codes: `2 class calm   FULLFLEX`), `● booked 3HT2NEIL` (green). A failed first add closes red (`● could not create the booking (…) · nothing was booked`), exit 1. A checkout failure closes red (`● booking 3HT2NEIL not checked out · provisional left, SJ releases it or cancel it on sj.se`), exit 1 — the `--book` cleanup covers only the configured route, so it is not promised here. A return leg that fails to add books the outbound alone (§8.2).

Config: `[search_parameters]` is required (`comfort_class`, `flexibility`, `allow_class_fallback`, `service_types`, `seat_preference` apply as in `--book`; `station_from`/`station_to`, `time_leave`/`time_return` and `roundtrip` are defaults only), `dates` is not (and is not validated for this mode, as for `--cancel-date`). The pass is the single valid one, or the one chosen at the existing numbered prompt; the typed dates are checked against it. Needs a terminal on stdin and stdout, dry run included: refused otherwise after auth and the travel-pass fetch, but before it asks anything (`● not a terminal · --book-journey asks questions`, exit 1); with several valid passes the pass prompt refuses even earlier (`● N valid travel passes · run in a terminal to choose one`, §9.1). Ctrl-D at any prompt, or Esc in a pick list, aborts (`● booking aborted, nothing was booked`, exit 1); Ctrl-C exits 130 with the pick-list frame wiped and the terminal restored.

## 6. Booking Workflow

### 6.1 Per-date flow

For each selected date (`dates`, §4.3) from today on. Steps 8–12 are one object, `booking.Cart` (`add` creates the provisional or adds a leg, `finish` does seats → customer → checkout); `--book`, the `--upgrade-class` re-book and `--book-journey` all write through it.

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
10. **Choose seats** (if `seat_preference` is set, §4.3): for each segment, read its seat map and pick the best free seat against the wish list — or, in `"ask"` mode, prompt for it — then write every chosen seat in one PATCH to the still-provisional booking (`_apply_seat_preference`, shared with the change-seat modes, §5.4). Never worth the booking: a map that will not load, no free seat to choose from, or a rejected PATCH each degrade to keeping the seat SJ assigned and print a `!` line rather than raise.
11. **Update customer details**: set the config email and the phone number the API already placed on the provisional (`customer.phoneNumber` of the create response, the passenger's as a fallback). Never a placeholder: with no number on the booking the field is left out.
12. **Checkout**: finalize the booking.

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

Departures already gone at search time are dropped first (`drop_departed`), before the closest-to-`time_leave` choice below and before the alternative of §8.1: a same-day run never books a train that has left. A departure timed exactly at now is kept, and so is one whose timestamp cannot be parsed.

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
- **Multiple valid passes**: `--book` and a real `--upgrade-class` name a window, so the single pass whose validity covers it is used without asking (a renewal bought ahead); `--book-journey` names no window, so several valid passes always reach the prompt. `--book` and `--book-journey` are the modes that prompt: a numbered list (`1. name (first day → last day)`) asks for a choice — at a terminal only (`● 2 valid travel passes · run in a terminal to choose one`, exit 1, otherwise) and an empty answer ends the run (`● no travel pass selected`). The remaining pass-scoped modes, `--upgrade-class` included, need only a date range and take the longest-lived pass silently (of those covering the window, when one is named); `--list-travelpasses` lists every pass, expired ones included (`(expired)`), and never selects.
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
- Two output families share one vocabulary. **Pass-scoped modes** (book, dry-run, cancel, list-bookings, list-travelpasses): open with the header box (`print_header_box`, rounded dim borders): `operation` (bold: `booking tickets` / `dry run · booking tickets` / `cancelling bookings` / `listing bookings` / `listing travel passes`), `account` (the configured login email — always the second row, app-wide), `travelpass`, `holder` (account+owner only for travel passes — the passes are the content). Book/dry-run follow with the `describe_run` facts block (`route`/`days`/`times`/`ticket`, card grammar), then dim progress trail (routine fetches are silent: spinner only), cards, and a closing ● status line (`pstatus`: green when something changed / dim when the run only reported — list footers and dry runs / red failed-or-aborted-or-nothing-found, dim text — cancel outcomes like `● booking X cancelled` / `● cancellation aborted` use the same line). **Auth modes** (login, logout, login-status): session-scoped and offline-capable, so no pass header — they open with a session-scoped header box instead (`operation` + `account`: config email for `--login`, cached profile_info email for `--logout`/`--login-status`), then any trail steps, then the status card (`print_status_card`: green/red dot + bold verdict, blank line, dim 7-char-padded labels with plain values, §5.8; `--login` ends by rendering the same card `--login-status` shows); travel passes render pass cards in the same fact grammar. No emoji anywhere in the output. Colour/bold/dim only on a TTY without `NO_COLOR`; the palette is the terminal's own bright ANSI slots (`91` red, `92` green, `93` yellow, `95` magenta, `96` cyan), never hardcoded RGB, so the output follows whatever theme the user runs. Every printed line starts with a one-space left margin (`_MARGIN` in `output`) so output sits off the terminal edge; blank lines stay empty.
- Prompts accept `y`/`yes` for confirmation, any case. Every input prompt is inline (answer typed on the same line) and marked with a cyan `?` — the SMS prompt via `prompt()`, all interactive choices/confirmations via `ask()` (both in `output`). A `?` question that asks about a block above it (a card or numbered list) is separated from that block by a blank line; a prompt that is itself a trail step (the SMS code) stays attached to its trail. Trail lines use human step names (`✓ performing login`, `✓ sending sms code`, `✓ completing login` — not OAuth plumbing terms) with the mark coloured — green `✓` on success, red `✗` on failure — and the step text dim. Glyph colours form one quartet: cyan `?` input needed, green `✓` step succeeded, red `✗` step failed, yellow `!` deviation worth noticing (`pwarn`: class fallbacks, time deviations, alternative-departure attempts, rejected SMS codes, checkout failures, missing departures, unmet seat wishes) — plus the `●` on verdict cards (green/red) and on every operation's closing status line (`pstatus`: green changed something, dim only reported, red failed). Marks are coloured, message text stays dim. A blank line separates a login trail from the card or title that follows.
- **Seat selection** (`seat_preference`, §4.3), shared by `--book` and the change-seat modes (§5.4): in `"ask"` mode, before prompting, `print_seat_choices` lists the free seats grouped by carriage — a dim `free in carriage N · <comfort> · N seats` header, then three per line as `number words` in the same vocabulary the config uses (e.g. `34 window, forward`) — followed by an inline `?`-marked prompt with the current seat as the default (`outbound seat [17]: `). An empty answer keeps that seat; typing a number not on the list re-asks (`! seat 12 is not free, pick one from the list`); Ctrl-D keeps the current seat and stops asking for the rest of the run; no terminal at all does the same for the whole run in one line (`! seat selection needs a terminal, keeping the seats SJ assigned`) and is the default outcome for a cron/`</dev/null` run. A ranked-list preference that cannot be fully honoured still takes the best remaining seat, naming the top wish it missed (`! outbound: no window seat free, taking carriage 3 seat 70 · table, forward`; a negated wish reads the other way round, `! outbound: no table-free seat, taking carriage 3 seat 70 · table, forward`); a seat map that will not load, a segment with nothing free to choose from, and an API response naming a different seat than the one asked for are each their own `!` line too. None of this ever fails a day, a booking or the run: a seat is never worth losing a booking over, so every failure here keeps whatever seat SJ assigned and moves on. `--list-bookings --seat-details` (§5.5) reads the same vocabulary read-only, to show what was assigned rather than to choose it — and, when `seat_preference` is a ranked word list, names a strictly better free seat when `best_seat`'s own ranking finds one, so it is obvious which legs are worth `--change-seat-date`/`--change-seat-booking`.

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

- **Ad-hoc trips outside `--book-journey`**: several legs stitched by hand (a journey SJ sells with changes is fine), several passengers.
- **Train-only station filter**: the picker prefers train-looking stations; it never hides a stop, so no switch is needed.
- **Multi-passenger booking**: always single passenger (the pass holder).
- **Dynamic station lookup in config**: config stations are validated against the hardcoded map; only `--book-journey` uses the live list.
- **SMS re-trigger**: user re-runs the tool if the SMS doesn't arrive (a mistyped code *is* retried, §3.2 step 8).
- **`dry_run` config key**: removed from config. `--dry-run` is a CLI modifier on `--book`/`--book-journey`/`--cancel-*`/`--change-seat-*`/`--upgrade-class`; the bare flags act for real.

## 12. Dependencies

- Python 3.13+
- `httpx` — HTTP client (the only runtime dependency)
- Declared in `pyproject.toml` (PEP 621) together with the `sj-cli` console script and the
  ruff/pytest/mypy configuration; install with `pip install -e .` (`--group dev` adds the dev tools)
- `pytest`, `ruff`, `mypy` (dev group only) — unit tests in `tests/`, no network
