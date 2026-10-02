# Digi-Fuel-Route — Project Memory

## Project Overview
Fuel route optimization API for truck drivers. Finds cheapest fuel stations along a route between two US locations, considering detour penalties. Built with Django 5.2 + DRF + httpx + NumPy.

## Tech Stack
- Python 3.12.3, Django 5.2.17, DRF 3.18.1, httpx 0.28.1, numpy 2.5.3
- pytest 9.1.1 + pytest-django, ruff 0.16.9
- SQLite (default), OSRM (routing), Nominatim (geocoding)
- No venv (python3-venv unavailable); packages installed via `pip3 install --user --break-system-packages`

## Architecture
- `config/` — Django project (settings, urls, wsgi, asgi)
- `routes/` — main app: models, services, management commands, tests
- `routes/services/importer.py` — CSV parsing, normalization, dedup
- `routes/services/geocoding.py` — Nominatim adapter with cache, retry, rate-limit
- `routes/services/routing.py` — OSRM adapter with cache, retry, validation
- `routes/services/geometry.py` — haversine, segment distance; NumPy vectorised _SegmentIndex for fast nearest-route-point (~0.5ms per station vs 74ms naive)
- `routes/services/optimizer.py` — Forward DP fuel-stop solver; O(n²) with fill-or-coast buy heuristic
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
- NumPy _SegmentIndex pre-extracts route arrays; each station query is vectorised np.argmin

## Optimizer Algorithm
- Forward DP on candidate DAG: O(n²) where n = corridor candidates
- "Fill-or-coast" heuristic: precompute first_cheaper[i] for O(1) buy decisions
- No path-in-heap; path reconstructed from dp_prev[] array
- Short route (<= initial fuel) returns immediately with 0 stops

## Performance Results (NY→Chicago, 12659-pt OSRM geometry, 224 candidates)
- Cold request: ~2.2s (2 Nominatim + 1 OSRM network calls)
- Warm request: ~430ms (DB cache only, zero external calls)
- Optimizer alone: ~340ms (NumPy vectorised geometry)

## Environment
- `.env` blocked by tool; use `env_example` file at root
- `python3 manage.py runserver` works; health endpoint at `/health/`
- write_file tool persists to disk reliably for this project

## Status (as of 2026-10-02 Phase 5 COMPLETE)
- Phase 0+1+2+3+4+5 complete — ALL DONE
- 77 tests passing, ruff clean, migrations applied
- API endpoint: POST /api/v1/routes/optimize/
- Response: route geometry, fuel_stops, summary, assumptions, metadata (cache flags)
- Database: 6,739 stations total, 6,614 geocoded (98.1%)
- README.md — comprehensive with benchmarks, setup, API reference
- Postman collection: postman/Fuel-Route-Optimization.postman_collection.json
- Remaining: Loom recording (manual task for user)
