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
- `routes/management/commands/import_fuel_prices.py` — CSV import command
- `routes/models.py` — FuelStation, GeocodeCache, RouteCache

## Key Decisions
- Dedup identity: `opis:{id}:rack:{rack}` preferred, fallback to `addr:{addr}:{city}:{state}`
- Cheapest price wins on duplicate identity
- Geocode cache: 720h TTL, route cache: 168h TTL
- Nominatim rate limit: 1 req/sec, exponential backoff (max 2 retries)
- OSRM corridor: 10 miles, max detour: 20 miles, penalty: $0.20/mile
- MPG=10, tank=50gal, max range=500mi

## Environment
- `.env` blocked by tool; use `env_example` file at root
- Nominatim public endpoint unreliable (timeouts); OSRM works fine
- `python3 manage.py runserver` works; health endpoint at `/health/`

## Status (as of 2026-10-01)
- Phase 0+1 complete: bootstrap, models, migrations, CSV importer, geocoding service
- 38 tests passing, ruff clean, migrations applied
- 425 stations imported from CSV (first 500 rows)
- Next: Phase 2 — OSRM routing adapter + route cache + optimization engine
