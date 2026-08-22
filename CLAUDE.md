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

# Dry run — search and display what would be booked, no booking made
python3 sj_tool.py --dry-run

# Book tickets for real — prints one result line per day
python3 sj_tool.py --book

# List all active bookings (one card per travel day)
python3 sj_tool.py --list-bookings

# List travel passes (validity, days left, price from receipt)
python3 sj_tool.py --list-travelpasses

# Cancel bookings on the configured route: one date, a comma list, and/or START..END ranges
python3 sj_tool.py --cancel-date 2026-01-20
python3 sj_tool.py --cancel-date 2026-01-20,2026-02-03..2026-02-05

# Cancel booking(s) by number, comma-separated, any case
python3 sj_tool.py --cancel-booking 3HT2NEIL,ABCD1234

# Authenticate and cache the token, then exit
python3 sj_tool.py --login

# Log out: end the sj.se session and delete cached token + cookies (next login needs SMS)
python3 sj_tool.py --logout

# Exit 0 if logged in (valid or refreshable cached token), 1 otherwise (for scripting; no config/network needed)
python3 sj_tool.py --login-status

# Verbose logging (logs go to stderr; secrets are redacted)
LOG_LEVEL=DEBUG python3 sj_tool.py --dry-run
LOG_LEVEL=TRACE python3 sj_tool.py --dry-run   # includes httpx request/response details
```

Log levels: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL. Default: CRITICAL (silent). Flags are mutually exclusive and one mode flag is required — a bare `python3 sj_tool.py` prints the help and exits 1; exit 0 success, 1 any failure, 130 Ctrl-C.

The tool requires interactive SMS input during first login (B2C MFA, 2-minute timeout). Subsequent runs use cached/refreshed tokens; cached SSO cookies let a later full login usually skip the SMS step.

A read-only smoke test against the live API that cannot block on the SMS prompt: `LOG_LEVEL=WARNING ./venv/bin/python sj_tool.py --dry-run </dev/null`, likewise `--list-bookings` / `--list-travelpasses`.

If `venv/` breaks (e.g. Homebrew Python was upgraded and the interpreter symlink dangles), recreate it:

```bash
rm -rf venv && python3 -m venv venv && ./venv/bin/pip install httpx typing_extensions pytest
```

## Repository layout

```
sj_*.py               one module per concern (table below)
tests/                pytest suite, no network (conftest.py: FakeClient, builders, base config)
config.example.toml   documented config template; the real config lives in ~/.config (never commit it)
config.toml           gitignored local copy — NOT read by the tool (it reads ~/.config/sj-api-client/)
SPEC.md, README.md, CLAUDE.md, ruff.toml, pytest.ini
curl-traces/          gitignored raw HAR/curl logs from reverse-engineering (contain tokens; grep, don't commit)
```

## Architecture

Modules organized by separation of concerns, plain venv (no package manager):

| Module | Role |
|---|---|
| `sj_tool.py` | Entry point. CLI argument parsing, top-level orchestration (`main()` → `_run()` so the HTTP client is always closed), travel-pass selection (expired passes ignored), date-vs-pass validation. |
| `sj_auth.py` | Authentication orchestration. Cookie-based silent login → B2C login flow with SMS → token lifecycle (validate → refresh → full login), proactive refresh, mid-run refresh (`ensure_valid_token`), logout (`handle_logout`: best-effort server-side end-session + cache clear). |
| `sj_client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic; every method raises on non-2xx. Constants for API base URLs/headers. `RetryTransport`: GET retried on 502/503/timeouts/connect errors (1s/2s/4s); POST/PATCH retried only on connect errors (request never sent). |
| `sj_booking.py` | Booking business logic. `process_date_range` → `process_booking_flow` (duplicate check, which search) → `handle_booking_process` (legs + checkout) → `_resolve_leg` (search → select → offers → one alternative; shared by dry-run and book mode). Also cancellation, listing, stale-provisional cleanup. |
| `sj_config.py` | Configuration loading and validation (`CfgManager`, collects all errors). `SERVICE_TYPE_NAMES` is the single source for valid service types + display names. |
| `sj_token.py` | Token cache management (`TokenManager`). Load (raises `SJAuthError` if corrupt), save, validate expiry, refresh availability; cookie cache next to it. |
| `sj_logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. `log_json()` redacts password/tokens/Authorization recursively — use it for any dict that might hold secrets. |
| `sj_errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`). |
| `sj_output.py` | User-facing output helpers. `pinfo()`/`pdim()`/`blank()`, `spinner()` (shares a stdout lock with pinfo so messages never interleave with frames; `trail=False` for silent waits), `indented()` nesting, ANSI styling (`style`/`pad`/`visible_len`; auto-off when not a TTY or `NO_COLOR` set), the shared day-card renderer (`day_header`, `print_day_note`, `leg_lines`/`print_leg_lines`, `print_bookings_table`), the status card (`print_status_card`, auth modes), the header box (`print_header_box`) and the travel-pass cards (`print_travelpasses`) — both built on the same dim-labelled `_fact_line` grammar. |
| `sj_calendar.py` | Swedish red-day calendar (no dependency; Easter computed). `skip_reason()` for weekend/holiday skipping. Timezone helpers `parse_api_datetime()` / `to_sweden()` / `sweden_now()` — all API timestamp handling goes through these (Swedish wall-clock for train times/pass validity, aware "now" for past/expiry). |

### Key design rules

- **No business logic in `sj_client.py`** — it exposes methods like `search_journey()`, `get_offers()`, etc. It does not interpret results or make decisions, and it raises (never returns an error body) on failure.
- **Thin entry point** — `sj_tool.py` parses CLI args, wires modules together, and delegates.
- **Errors as typed exceptions** — `SJAPIError`, `SJAuthError`, `SJConfigError`. Catch at the orchestration layer; `process_date_range` catches per date so one bad day doesn't stop the run.
- **Booking-flow contract** — `process_date_range` → `plan_day()` (which legs a day still needs) → `process_booking_flow(…, need_outbound, need_inbound)` → `handle_booking_process(client, token, cfg, passenger_token, out_search_id, in_search_id, dry_run)`: a leg is handled iff its search id is passed. Dry run returns `{"outbound"/"inbound": {departure, arrival, duration, train, route, class, flexibility, has_offer}}`; book mode returns `{"booking_id", "booking_number", "legs", "checked_out", "booking"}` (the API's booking object, with journeys, for the card) or `None` when nothing was booked (a checkout failure still returns the dict, with `checked_out=False`, so the `book_partial` fallback doesn't create a second provisional). `process_date_range` renders the day card from this.
- **Stale provisionals** — `is_stale_provisional()` (status NEW + CANCEL_JOURNEY) is used by both the `--book` cleanup and the duplicate check, so dry run and book agree on what is already booked.
- **Dates/times** — never parse API timestamps by hand; use `sj_calendar` helpers. Config times are Swedish local.
- **Output style** — two families, one vocabulary. Pass-scoped modes (book/dry-run/cancel/list-bookings/list-travelpasses): header box (`print_header_box`: operation/travelpass/holder, rounded dim borders, bold operation) → for book/dry-run a `describe_run()` facts block (route/days/times/ticket via `print_fact`) → dim progress trail (routine fetches silent via `spinner(trail=False)`) → one card per day (`print_day_header`/`print_day_note` + `print_leg_lines`, nested with `indented()`) → closing ● status line (`pstatus`: green/red by outcome, dim text; cancel outcomes and list footers alike). Auth modes (login/logout/login-status): session-scoped, no pass header — they render a status card (`print_status_card`: green/red dot + bold verdict + dim-labelled facts; `--login` ends with the same card `--login-status` prints), the list modes their day/pass cards. Prose is lowercase; identifiers keep their case (booking numbers UPPER, station names as SJ writes them) — `pinfo` no longer lowercases anything. Restrained colour, no emoji anywhere. Every input prompt is inline with a cyan `?` marker (`prompt()`/`ask()` in `sj_output`). No redundant tags; booking numbers on every leg line. Spinner trail lines for real steps (green `✓` / red `✗` mark, dim text), `trail=False` for waits; deviations print via `pwarn` (yellow `!` mark, dim text).
- **Secrets** — never log raw config/token dicts or request payloads with f-strings; go through `log_json()`.

## Configuration

Config path: `~/.config/sj-api-client/config.toml` (or `$XDG_CONFIG_HOME/sj-api-client/config.toml`). Template: `config.example.toml`.

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

- Python 3.13+ (uses `tomllib` and `zoneinfo` from stdlib, `type | None` union syntax)
- `httpx` — HTTP client
- `typing_extensions` — `@override` decorator
- `pytest` — tests only
- No requirements.txt/pyproject.toml; install manually: `pip install httpx typing_extensions pytest`

## Tests and lint

```bash
./venv/bin/pytest               # ~140 tests, <1s, no network
ruff check .                    # lint (ruff.toml selects ALL with documented ignores; tests have their own)
```

Tests live in `tests/` and never touch the network: `tests/conftest.py` provides a scripted `FakeClient` (records every API call), departure/offer builders and a base config. `tests/test_booking_flow.py` pins the booking flow's API call sequence, return contract and user messages — run it after any change to `sj_booking.py`; `tests/test_dates.py` pins the timezone rules; `tests/test_client.py` the retry policy; `tests/test_cli.py` the flag contract and the --cancel-date/--cancel-booking validate-first parsers; `tests/test_auth.py` the login flow's trail/prompt/SMS-retry behaviour; `tests/test_logout.py` and `tests/test_login_status.py` the auth modes' output and logic; `tests/test_output.py` the shared renderers (cards, header box, status/trail/warn lines, prompts). There is no CI.

Definition of done for a change: `ruff check .` clean, `pytest` green, and — for anything touching auth, client or booking — a live dry run (`</dev/null`) that exits 0. A real `--book` can only be verified by the user.

## Security notes

- The real config (with the SJ password) must stay in `~/.config`; `config.toml` in the repo is gitignored. Git history was scrubbed of it on 2026-08-19 with `git filter-repo` — keep it that way.
- `curl-traces/` holds raw request/response logs with live tokens; it is gitignored.
- DEBUG/TRACE logs are redacted, but the raw token/cookie cache files are not — treat `~/.cache/sj-api-client/` as sensitive.
