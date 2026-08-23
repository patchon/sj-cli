# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# sj-api-client

Python CLI tool for automated SJ (Swedish Railways) train ticket booking using travel pass benefits (e.g., SJ Annual Card). Reverse-engineers the sj.se web app's API to authenticate, search, and book journeys.

- `README.md` — user-facing: setup, config, usage, how a day is booked.
- `SPEC.md` — full specification (edge cases, error handling, CLI modes, validation rules, output format, retry and timezone rules).
- This file — how the code is organised and how to work on it.

## Running the application

```bash
source venv/bin/activate

# Preview a booking run — --dry-run modifies --book and the cancel flags (nothing real happens)
sj-tool --book --dry-run

# Book tickets for real — prints one result line per day
sj-tool --book

# List all active bookings (one card per travel day)
sj-tool --list-bookings

# List travel passes (validity, days left, price from receipt)
sj-tool --list-travelpasses

# Cancel that day's journeys on the configured route (other days of a booking are kept): one date,
# an ISO week, a comma list, and/or START..END ranges
sj-tool --cancel-date 2026-01-20
sj-tool --cancel-date 2026-01-20,2026-02-03..2026-02-05
sj-tool --cancel-date W43   # a whole ISO week
sj-tool --cancel-date 2026-01-20 --dry-run   # preview: cards + would-cancel status, no prompts

# Cancel booking(s) by number, comma-separated, any case
sj-tool --cancel-booking 3HT2NEIL,ABCD1234

# Authenticate and cache the token, then exit
sj-tool --login

# Log out: end the sj.se session and delete cached token + cookies (next login needs SMS)
sj-tool --logout

# Exit 0 if logged in (valid or refreshable cached token), 1 otherwise (for scripting; no config/network needed)
sj-tool --login-status

# Verbose logging (logs go to stderr; secrets are redacted)
LOG_LEVEL=DEBUG sj-tool --book --dry-run
LOG_LEVEL=TRACE sj-tool --book --dry-run   # includes httpx request/response details
```

Log levels: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL. Default: CRITICAL (silent). Flags are mutually exclusive and one mode flag is required — a bare `sj-tool` prints the help and exits 1; exit 0 success, 1 any failure, 130 Ctrl-C. `sj-tool` is the console script installed by `pip install -e .` (with the venv activated, or `./venv/bin/sj-tool`); `python -m sj_api_client` is equivalent.

The tool requires interactive SMS input during first login (B2C MFA, 2-minute timeout). Subsequent runs use cached/refreshed tokens; cached SSO cookies let a later full login usually skip the SMS step.

A read-only smoke test against the live API that cannot block on the SMS prompt: `LOG_LEVEL=WARNING ./venv/bin/sj-tool --book --dry-run </dev/null`, likewise `--list-bookings` / `--list-travelpasses`.

If `venv/` breaks (e.g. Homebrew Python was upgraded and the interpreter symlink dangles), recreate it:

```bash
rm -rf venv && python3 -m venv venv && ./venv/bin/pip install -e . --group dev
```

## Repository layout

```
src/sj_api_client/    the package (standard src layout): one module per concern (table below),
                      __main__.py for `python -m sj_api_client`, config.example.toml (package data:
                      the documented config template; the real config lives in ~/.config, never commit it)
tests/                pytest suite, no network (conftest.py: autouse fixture; fakes.py: FakeClient, builders, base config)
pyproject.toml        project metadata, dependencies, console script and the ruff/pytest/mypy configuration
config.toml           gitignored local copy — NOT read by the tool (it reads ~/.config/sj-api-client/)
SPEC.md, README.md, CLAUDE.md
curl-traces/          gitignored raw HAR/curl logs from reverse-engineering (contain tokens; grep, don't commit)
```

## Architecture

Modules in `src/sj_api_client/`, organized by separation of concerns; imports are absolute (`from sj_api_client.errors import ...`):

| Module | Role |
|---|---|
| `cli.py` | Entry point. CLI argument parsing (`allow_abbrev=False`: no prefix aliases), top-level orchestration (`main()` → `_run()` so the HTTP client is always closed; every failure exit ends with a red `●` line via `pstatus`), travel-pass selection (`resolve_travel_pass`: expired passes ignored, the pass covering the booking window wins, prompts only at a terminal, listing/cancel modes never prompt), `booking_window()` (first/last bookable selected date from today on, None when all are skipped) and date-vs-pass validation on it. |
| `auth.py` | Authentication orchestration. Cookie-based silent login → B2C login flow with SMS (`read_sms_code`: empty lines re-asked, timeout only at a terminal, pipes read line by line) → token lifecycle (validate → refresh → full login: a refresh that fails *transiently* raises `SJAuthError` instead of falling back to an SMS login; only a rejected refresh token — `refresh_token()` returning None — does), proactive refresh, mid-run refresh (`ensure_valid_token`), logout (`handle_logout`: best-effort server-side end-session + cache clear). |
| `client.py` | HTTP client. All SJ API communication via `httpx.Client` (`HTTP_TIMEOUT`: 30 s, 10 s connect). Low-level request methods only — no business logic; every method raises on failure: data endpoints through `_json_or_raise` (error envelope beats the status even behind a 200, empty/non-JSON 2xx is an error), the cancel/revert/confirm methods and the B2C fingerprint POSTs through `_raise_for_failure` (empty/HTML 2xx fine); callers print the cause. `refresh_token()`: dict on success, None when B2C rejects the refresh token, `SJAuthError` on transient failures (the POST opts into the GET retry policy via `RETRY_EXTENSION`). Constants for API base URLs/headers. `RetryTransport`: GET retried on 502/503/timeouts/connection errors/resets (1s/2s/4s); POST/PATCH retried only on connect errors (request never sent); the body is read inside the loop (`_read_fully`: raw bytes, fresh response — a stalled body is that attempt's error, a retried 503 releases its connection). |
| `booking.py` | Booking business logic. `process_date_range` (walks `selected_dates()` from `today` on, dropping past days with a note, contains a mid-run refresh failure, a planning or rendering failure to the day) → `process_booking_flow` (duplicate check, which search) → `handle_booking_process` (legs + checkout; the customer PATCH sends back the phone the API put on the provisional, never a placeholder) → `_resolve_leg` (search → select → offers → one alternative; shared by dry-run and book mode). Also cancellation (`handle_cancel_booking(only_date=…)` for `--cancel-date`), listing, stale-provisional cleanup (`cleanup_stale_provisionals(route=…, now=…)`: configured route + older than `STALE_PROVISIONAL_GRACE`). |
| `config.py` | Configuration loading and validation (`CfgManager`, collects all errors; `verify_cfg(require_search=…, require_dates=…)`). `SERVICE_TYPE_NAMES` is the single source for valid service types + display names. `_validate_dates`: the `dates` selection (grammar in `dates.parse_date_selection`), normalised in place, old `date_start`/`date_end` keys rejected with a migration hint. |
| `tokens.py` | Token cache management (`TokenManager`). Load (raises `SJAuthError` if corrupt or not an object), save (atomic replace via a per-writer `mkstemp` temp file, files 0600, directory 0700; the in-memory token is kept even if the write fails and `save_error` says why), validate expiry, refresh availability; cookie cache next to it (a malformed one is ignored with a warning). |
| `logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. `log_json()` redacts password/tokens/Authorization/SMS code/CSRF/long `code` values recursively — use it for any dict that might hold secrets; httpx/httpcore TRACE lines and logged URLs are scrubbed of cookie/auth header values and `code=` (`redact_url`). |
| `errors.py` | Custom exceptions: `SJError` base, `SJAPIError` (unexpected/erroneous API response; reads `errorCode`/`error`/`code`, `message` and `validationErrors`), `SJAuthError` (login/token lifecycle), `SJConfigError`. `error_text(e)`: the one-line, URL-redacted, never-empty text every printed exception goes through. Nothing raises a bare `Exception`. |
| `output.py` | User-facing output helpers. `pinfo()`/`pdim()`/`blank()`, `spinner()` (shares a stdout lock with pinfo so messages never interleave with frames; `trail=False` for silent waits), `indented()` nesting, ANSI styling (`style`/`pad`/`visible_len`; auto-off when not a TTY or `NO_COLOR` set), the shared day-card renderer (`day_header`, `print_day_note`, `leg_lines`/`print_leg_lines`, `print_bookings_table`), the status card (`print_status_card`, auth modes), the header box (`print_header_box`) and the travel-pass cards (`print_travelpasses`) — both built on the same dim-labelled `_fact_line` grammar. |
| `dates.py` | Swedish red-day calendar (no dependency; Easter computed). `skip_reason()` for weekend/holiday skipping. Timezone helpers `parse_api_datetime()` / `to_sweden()` / `sweden_now()` — all API timestamp handling goes through these (Swedish wall-clock for train times/pass validity, aware "now" for past/expiry). The date-selection grammar `parse_date_selection()` (dates, ISO weeks `W43`/`2027-W02`, `start..end` ranges; shared by the `dates` key and `--cancel-date`) and `selected_dates()`/`booking_dates()` (the validated selection, all / from today on). |

### Key design rules

- **No business logic in `client.py`** — it exposes methods like `search_journey()`, `get_offers()`, etc. It does not interpret results or make decisions, and it raises (never returns an error body) on failure.
- **Thin entry point** — `cli.py` parses CLI args, wires modules together, and delegates.
- **Errors as typed exceptions** — `SJAPIError`, `SJAuthError`, `SJConfigError`. Catch at the orchestration layer; `process_date_range` catches per date so one bad day doesn't stop the run.
- **Booking-flow contract** — `process_date_range` → `plan_day()` (which legs a day still needs) → `process_booking_flow(…, need_outbound, need_inbound)` → `handle_booking_process(client, token, cfg, passenger_token, out_search_id, in_search_id, dry_run)`: a leg is handled iff its search id is passed. Dry run returns `{"outbound"/"inbound": {departure, arrival, duration, train, route, class, flexibility, has_offer}}`; book mode returns `{"booking_id", "booking_number", "legs", "checked_out", "booking"}` (the API's booking object, with journeys, for the card) or `None` when nothing was booked (a checkout failure still returns the dict, with `checked_out=False`, so the `book_partial` fallback doesn't create a second provisional). `process_date_range` renders the day card from this and returns the day counts; `cli` maps `failed`/`error` to exit 1. `handle_cancel_mode`/`handle_cancel_booking` return `bool` for the same purpose (SPEC §5.7).
- **Stale provisionals** — `is_stale_provisional()` (status NEW + CANCEL_JOURNEY) is used by the `--book` cleanup and, via `is_active_booking()`, by the duplicate check, `--list-bookings` and both cancel modes, so every mode agrees on what is a booking. The cleanup cancels only provisionals on the configured route that are older than `STALE_PROVISIONAL_GRACE` (a cart the user has open, or a concurrent run, is not ours to cancel). The duplicate check matches whole journeys (`_journey_endpoints`) on the Swedish date (`_segment_date`).
- **Cancel scope** — `--cancel-date D` hands `only_date=D` to `handle_cancel_booking`, which shows and cancels that day's journeys only; a booking's other days are reported as kept.
- **API nulls** — the API writes explicit `null` for absent sub-objects: read them as `(x.get(key) or {})` / `or []`, never `.get(key, {})`.
- **Printing errors** — every `{e}` shown to the user goes through `error_text(e)` (one line, auth codes redacted, type name when the message is empty).
- **Dates/times** — never parse API timestamps by hand; use `dates` helpers. Config times are Swedish local. Selected dates already gone are not an error: `booking_dates()`/`process_date_range(today=…)` start from today (tests pass a fixed `today`).
- **Output style** — two families, one vocabulary. Pass-scoped modes (book/dry-run/cancel/list-bookings/list-travelpasses): header box (`print_header_box`: operation/travelpass/holder, rounded dim borders, bold operation) → for book/dry-run a `describe_run()` facts block (route/days/times/ticket via `print_fact`) → dim progress trail (routine fetches silent via `spinner(trail=False)`) → one card per day (`print_day_header`/`print_day_note` + `print_leg_lines`, nested with `indented()`) → closing ● status line (`pstatus`: green/red by outcome, dim text; cancel outcomes and list footers alike). Auth modes (login/logout/login-status): session-scoped, no pass header — they render a status card (`print_status_card`: green/red dot + bold verdict + dim-labelled facts; `--login` ends with the same card `--login-status` prints), the list modes their day/pass cards. Prose is lowercase; identifiers keep their case (booking numbers UPPER, station names as SJ writes them) — `pinfo` no longer lowercases anything. Restrained colour, no emoji anywhere. Every input prompt is inline with a cyan `?` marker (`prompt()`/`ask()` in `output`). No redundant tags; booking numbers on every leg line. Spinner trail lines for real steps (green `✓` / red `✗` mark, dim text), `trail=False` for waits; deviations print via `pwarn` (yellow `!` mark, dim text).
- **Secrets** — never log raw config/token dicts or request payloads with f-strings; go through `log_json()`.

## Configuration

Config path: `~/.config/sj-api-client/config.toml` (or `$XDG_CONFIG_HOME/sj-api-client/config.toml`). Template: `src/sj_api_client/config.example.toml` (package data next to `config.py`, read by first-run setup). Missing config: `--login` on a terminal (stdin and stdout) → first-run setup (`CfgManager.create_interactive`): asks credentials, writes the template with them filled in (0600, TOML-escaped via a function replacement — a string `re.sub` replacement would mangle backslashes), then logs in; any other operation, a non-interactive run, or a declined offer → `● no configuration` card, exit 1 (a write failure → `● config not created`). `verify_cfg(cfg, require_search=..., require_dates=...)`: [auth] always, [search_parameters] only for --book/--cancel-date, the `dates` selection only for --book.

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"  # dates and/or ISO weeks ("W36, W38..40"); past days are skipped (a selection entirely in the past is an error)
time_leave = "05:29"
time_return = "17:22"
station_from = "Göteborg Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"    # "1 class", "2 class", "2 class calm"
flexibility = "FULLFLEX"          # FULLFLEX, SEMIFLEX, NOFLEX
roundtrip = true
select_closest_ticket_available = true
allow_class_fallback = true       # optional, defaults to true
book_partial = false              # optional; if the outbound leg is unavailable, still book the return leg separately (see SPEC §6.5)
skip_weekends = true              # optional, defaults to true
skip_holidays = true              # optional, defaults to true — Swedish red days incl. Midsommar-/Jul-/Nyårsafton
service_types = ["SJ_HIGH", "SJ_IC"]  # optional, filter train types (omit or ["ALL"] for no filter)
# Valid: ALL, SJ_HIGH, SJ_IC, SJ_REG, SJ_NT, X_TRAINOPS, X_PTA, X_EXPBUS
```

All fields are validated at startup before any API calls. See SPEC.md §4.3 for full validation rules.

Caches: `~/.cache/sj-api-client/token.json` (tokens) and `cookies.json` (SSO cookies), auto-created on first login; delete the directory to force a fresh login.

## Dependencies

Declared in `pyproject.toml` (PEP 621; dev tools in a PEP 735 dependency group):

- Python 3.13+ (`tomllib`, `zoneinfo`, `typing.override`, `type | None` syntax)
- `httpx` — HTTP client (the only runtime dependency)
- dev group: `pytest`, `ruff`, `mypy`

Install into the venv with `./venv/bin/pip install -e . --group dev` (editable install also provides the `sj-tool` console script). Standard src layout: `[tool.setuptools.packages.find] where = ["src"]` discovers the package, `config.example.toml` ships as package data, so the tool runs only installed (editable or not) — never from the working tree directly.

## Tests and lint

```bash
./venv/bin/pytest               # ~250 tests, <1s, no network
./venv/bin/ruff check .         # lint (pyproject selects ALL with documented ignores; tests have their own)
./venv/bin/ruff format --check . # formatting (run `ruff format .` to apply)
./venv/bin/mypy                 # type check (the package fully, tests' annotated parts)
```

All three tools read their configuration from `pyproject.toml`. Tests live in `tests/` and never touch the network: `tests/fakes.py` provides a scripted `FakeClient` (records every API call and customer update), departure/offer builders, a base config (fixed 2026-09 dates for the flow fixtures) and `future_cfg()` (a window that validates whatever today is — use it wherever `verify_cfg` runs); `tests/conftest.py` holds the autouse fixture (no colour, no sleeping). `tests/test_booking_flow.py` pins the booking flow's API call sequence, return contract and user messages — run it after any change to `booking.py`; `tests/test_dates.py` pins the timezone rules; `tests/test_client.py` the retry policy; `tests/test_cli.py` the flag contract and the --cancel-date/--cancel-booking validate-first parsers; `tests/test_auth.py` the login flow's trail/prompt/SMS-retry behaviour; `tests/test_logout.py` and `tests/test_login_status.py` the auth modes' output and logic; `tests/test_output.py` the shared renderers (cards, header box, status/trail/warn lines, prompts). There is no CI.

Definition of done for a change: `ruff check .`, `ruff format --check .` and `mypy` clean, `pytest` green, and — for anything touching auth, client or booking — a live dry run (`</dev/null`) that exits 0. A real `--book` can only be verified by the user.

## Security notes

- The real config (with the SJ password) must stay in `~/.config`; `config.toml` in the repo is gitignored. Git history was scrubbed of it on 2026-08-19 with `git filter-repo` — keep it that way.
- `curl-traces/` holds raw request/response logs with live tokens; it is gitignored.
- DEBUG/TRACE logs are redacted, but the raw token/cookie cache files are not — they are written owner-only (0600 in a 0700 directory); still treat `~/.cache/sj-api-client/` as sensitive.
