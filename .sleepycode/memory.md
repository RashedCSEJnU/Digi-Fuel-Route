# Digi-Fuel-Route — Project Memory

## Project Overview
Fuel route optimization API for truck drivers. Finds cheapest fuel stations along a route between two US locations, considering detour penalties. Built with Django 5.2 + DRF + httpx.

## Tech Stack
- Python 3.12.3, Django 5.2.17, DRF 3.18.1, httpx 0.28.1
- pytest 9.1.1 + pytest-django, ruff 0.16.9
- SQLite (default), OSRM (routing), Nominatim (geocoding)
- No venv (python3-venv unavailable); packages installed via `pip3 install --user --break-system-packages`

## Architecture
- `config/` — Django project (settings, urls, wsgi, asgi)
- `routes/` — main app: models, services, management commands, tests
- `routes/services/importer.py` — CSV parsing, normalization, dedup
- `routes/services/geocoding.py` — Nominatim adapter with cache, retry, rate-limit
- `routes/services/routing.py` — OSRM adapter with cache, retry, validation
- `routes/services/geometry.py` — haversine, segment distance (Decimal-safe), bounding box filter
- `routes/services/optimizer.py` — DP fuel-stop solver
- `routes/exceptions.py` — canonical domain exceptions + DRF exception handler
- `routes/serializers.py` — DRF request serializer (start, finish, initial_fuel_gallons)
- `routes/views.py` — OptimizeRouteView orchestration: validate→geocode→route→optimize→serialize
- `routes/management/commands/import_fuel_prices.py` — CSV import command
- `routes/models.py` — FuelStation, GeocodeCache, RouteCache

## Key Decisions
- Dedup identity: `opis:{id}:rack:{rack}` preferred, fallback to `addr:{addr}:{city}:{state}`
- Cheapest price wins on duplicate identity
- Geocode cache: 720h TTL, route cache: 168h TTL
- Nominatim rate limit: 1 req/sec, exponential backoff (max 2 retries)
- OSRM corridor: 10 miles, max detour: 20 miles, penalty: $0.20/mile
- MPG=10, tank=50gal, max range=500mi
- Route cache key: SHA256 of `origin->dest:profile` (directional)
- Canonical domain exceptions: all in `routes/exceptions.py`; geocoding.py imports NonUSLocationError from there (re-exported for backward compat)
- DRF exception handler: NonUSLocationError→400, NoFeasiblePlanError→404, ProviderError→502
- Geometry functions cast all coords to float() to handle Django Decimal model fields

## Environment
- `.env` blocked by tool; use `env_example` file at root
- Nominatim public endpoint unreliable (timeouts); OSRM works fine
- `python3 manage.py runserver` works; health endpoint at `/health/`
- write_file tool does NOT persist to disk reliably; always use run_command cat > file << 'PYEOF' for new/rewritten files

## Status (as of 2026-10-02)
- Phase 0+1+2+3+4 complete
- 77 tests passing, ruff clean, migrations applied
- API endpoint: POST /api/v1/routes/optimize/
- Response: route geometry, fuel_stops, summary, assumptions, metadata (cache flags)
- Next: Phase 5 — performance measurement, documentation, Postman collection
