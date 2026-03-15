# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# sj-api-client

Python CLI tool for automated SJ (Swedish Railways) train ticket booking using travel pass benefits (e.g., SJ Annual Card). Reverse-engineers the sj.se web app's API to authenticate, search, and book journeys.

See `SPEC.md` for the full specification (edge cases, error handling, CLI modes, validation rules, output format).

## Running the application

```bash
source venv/bin/activate

# Book tickets (default mode)
python3 sj_tool.py

# Dry run — search and display what would be booked
python3 sj_tool.py --dry-run

# List current bookings
python3 sj_tool.py --list-current-bookings

# Cancel a specific date
python3 sj_tool.py --cancel-date 2026-01-20

# Verbose logging (logs go to stderr)
LOG_LEVEL=DEBUG python3 sj_tool.py
LOG_LEVEL=TRACE python3 sj_tool.py   # includes httpx request/response details
```

Log levels: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL. Default: CRITICAL (silent).

The tool requires interactive SMS input during first login (B2C MFA, 2-minute timeout). Subsequent runs use cached/refreshed tokens.

## Architecture

Modules organized by separation of concerns, plain venv (no package manager):

| Module | Role |
|---|---|
| `sj_tool.py` | Entry point. CLI argument parsing, top-level orchestration. |
| `sj_auth.py` | Authentication orchestration. B2C login flow, token lifecycle (validate → refresh → full login), SMS input with timeout. |
| `sj_client.py` | HTTP client. All SJ API communication via `httpx.Client`. Low-level request methods only — no business logic. Includes HTTP retry logic (3 retries, exponential backoff for transient errors). |
| `sj_booking.py` | Booking business logic. Search, departure selection, offer matching, provisional booking creation, checkout, cancellation, duplicate detection. |
| `sj_config.py` | Configuration loading and validation (`CfgManager`). |
| `sj_token.py` | Token cache management (`TokenManager`). Load, save, validate expiry, check refresh availability. |
| `sj_logger.py` | Logging setup with custom TRACE level, color formatter, httpx log filtering. |
| `sj_errors.py` | Custom exceptions (`SJAPIError`, `SJAuthError`, `SJConfigError`). |
| `sj_output.py` | User-facing output helpers. `pinfo()`, table formatting for dry-run results and booking listings. |

### Key design rules

- **No business logic in `sj_client.py`** — it exposes methods like `search_journey()`, `get_offers()`, etc. It does not interpret results or make decisions.
- **Thin entry point** — `sj_tool.py` parses CLI args, wires modules together, and delegates.
- **Errors as typed exceptions** — `SJAPIError`, `SJAuthError`, `SJConfigError`. Catch at the orchestration layer.

## Configuration

Config path: `~/.config/sj-api-client/config.toml` (or `$XDG_CONFIG_HOME/sj-api-client/config.toml`)

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
book_partial = false              # optional, continue booking if one leg fails
service_types = ["SJ_HIGH", "SJ_IC"]  # optional, filter train types (omit or ["ALL"] for no filter)
# Valid: ALL, SJ_HIGH, SJ_IC, SJ_REG, SJ_NT, X_TRAINOPS, X_PTA, X_EXPBUS
```

All fields are validated at startup before any API calls. See SPEC.md §4.3 for full validation rules.

Token cache: `~/.cache/sj-api-client/token.json` (auto-created on first login)

## Dependencies

- Python 3.13+ (uses `tomllib` from stdlib, `type | None` union syntax)
- `httpx` — HTTP client
- `typing_extensions` — `@override` decorator
- No requirements.txt/pyproject.toml; install manually: `pip install httpx typing_extensions`

## Tests and CI

There are no tests or CI configuration in this project.
