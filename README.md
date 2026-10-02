# Digi-Fuel-Route — Fuel Route Optimization API

A fast Django REST API that accepts two US locations and returns the optimal driving route with cost-effective fuel stop recommendations, considering real truck-stop prices from the supplied CSV dataset.

---

## Problem statement

Given a start and finish location (both within the USA), return:

- A map-ready GeoJSON `LineString` of the driving route
- Optimal fuel stops along the route (cheapest fuel, accounting for detour cost)
- All stops assume the vehicle has a **500-mile maximum range** (50-gallon tank, 10 MPG)
- Total money spent on fuel for the trip

---

## Fixed vehicle assumptions

| Assumption              | Value                |
|-------------------------|----------------------|
| Fuel economy            | 10 miles per gallon  |
| Tank capacity           | 50 gallons           |
| Maximum range per tank  | 500 miles            |
| Starting fuel level     | 50 gallons (default) |
| Pricing source          | `fuel-prices-for-be-assessment.csv` |

---

## Architecture

```
POST /api/v1/routes/optimize/
        │
        ▼
  ① Geocode start (Nominatim) ──► GeocodeCache (SQLite)
  ② Geocode finish (Nominatim) ──► GeocodeCache (SQLite)
        │
        ▼
  ③ Get driving route (OSRM) ──────► RouteCache (SQLite)
        │                    (1 API call max on cache miss)
        ▼
  ④ Load resolved FuelStations from DB  (0 external calls)
        │
        ▼
  ⑤ Build corridor candidates   ← NumPy vectorised spatial index
        │                           bounding-box pre-filter → nearest segment
        ▼
  ⑥ Forward DP optimiser        ← O(n²) DAG shortest-path
        │                           "fill-or-coast" buy heuristic
        ▼
  ⑦ Serialize & return JSON response
```

**Provider call budget:**

| Scenario           | Geocode calls | Route calls |
|--------------------|--------------|-------------|
| Cold (no cache)    | 2 max        | 1           |
| Warm (cached)      | 0            | 0           |

---

## Tech stack

| Concern    | Choice                                      |
|------------|---------------------------------------------|
| Framework  | Django 5.2.17 + Django REST Framework 3.18  |
| Python     | 3.12.3                                      |
| Database   | SQLite (zero-setup; holds all cached data)  |
| HTTP client| httpx (explicit timeouts, retry, pooling)   |
| Geocoding  | Nominatim / OpenStreetMap (free, no key)    |
| Routing    | Public OSRM (free, no key)                  |
| Geometry   | NumPy vectorised segment index + haversine  |
| Testing    | pytest + pytest-django (107 tests)           |
| Linting    | Ruff                                        |

---

## Prerequisites

- Python 3.12+
- `pip3`

---

## Setup

```bash
# 1. Clone
git clone <repo-url>
cd Digi-Fuel-Route

# 2. Install dependencies
pip3 install --user --break-system-packages \
    django==5.2.17 djangorestframework==3.18.1 httpx==0.28.1 \
    pytest==9.1.1 pytest-django==4.14.0 ruff==0.16.9 numpy

# 3. Copy environment config
cp .env.example .env.local   # edit values as needed (see table below)

# 4. Apply migrations
python3 manage.py migrate

# 5. Import fuel prices (required before first route request)
python3 manage.py import_fuel_prices fuel-prices-for-be-assessment.csv

# 6. Geocode stations (required for route optimization)
#    This resolves station lat/lon via Nominatim at 1 req/sec.
#    Full run takes several hours; a cached db.sqlite3 is included.
#    Skip this step if db.sqlite3 already contains resolved stations.
python3 manage.py geocode_stations

# 7. Run the development server
python3 manage.py runserver
```

The server starts at `http://localhost:8000`.

---

## Import command

```bash
# Dry run — validates CSV without writing to DB
python3 manage.py import_fuel_prices fuel-prices-for-be-assessment.csv --dry-run

# Full import
python3 manage.py import_fuel_prices fuel-prices-for-be-assessment.csv

# Import with JSON quality report
python3 manage.py import_fuel_prices fuel-prices-for-be-assessment.csv \
    --report /tmp/import_report.json
```

**CSV quality**: the supplied file has 8,151 data rows. After deduplication by
OPIS ID + Rack ID (keeping the cheapest price per identity), 6,739 unique
stations are stored. Of those, 6,614 are successfully geocoded (98.1%).
The 125 unresolved stations were not found by the public Nominatim geocoder
and are excluded from route optimization.

---

## API reference

### `GET /health/`

Liveness check. Returns `200 {"status": "ok"}`. Never calls external providers.

```bash
curl http://localhost:8000/health/
# {"status": "ok"}
```

---

### `POST /api/v1/routes/optimize/`

**Request body:**

```json
{
  "start": "New York, NY",
  "finish": "Chicago, IL",
  "initial_fuel_gallons": 50
}
```

| Field                  | Type    | Required | Description |
|------------------------|---------|----------|-------------|
| `start`                | string  | ✅       | US start location |
| `finish`               | string  | ✅       | US finish location |
| `initial_fuel_gallons` | number  | ❌       | Starting fuel (0–50, default 50) |

**Success response `200`:**

```json
{
  "route": {
    "distance_miles": 790.57,
    "trip_distance_miles": 798.74,
    "duration_seconds": 41693.5,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-74.006, 40.7128], "..."]
    }
  },
  "assumptions": {
    "max_range_miles": 500,
    "mpg": 10,
    "tank_capacity_gallons": 50,
    "initial_fuel_gallons": 50,
    "fuel_price_unit": "USD per gallon",
    "detour_distance_method": "estimated round-trip distance from nearest route point"
  },
  "fuel_stops": [
    {
      "sequence": 1,
      "station": {
        "id": 2847,
        "name": "TURNPIKE MARKET EXPRESS",
        "address": "2801 NEWTON FALLS BAINBRIDGE RD",
        "city": "Newton Falls",
        "state": "OH",
        "coordinates": {"latitude": 41.1878, "longitude": -81.0013}
      },
      "price_per_gallon": 3.259,
      "route_mile": 349.55,
      "detour_miles": 0.36,
      "fuel_before_stop_gallons": 14.91,
      "gallons_purchased": 7.13,
      "fuel_after_stop_gallons": 22.04,
      "estimated_cost": 23.24
    }
  ],
  "summary": {
    "total_fuel_consumed_gallons": 79.87,
    "total_gallons_purchased": 29.95,
    "total_fuel_cost": 101.42,
    "currency": "USD"
  },
  "metadata": {
    "geocoder_provider": "Nominatim",
    "route_provider": "OSRM",
    "cached_geocodes": {"start": false, "finish": false},
    "cached_route": false,
    "station_count_considered": 224,
    "optimizer_version": "v1"
  }
}
```

**Error responses:**

| Status | Situation | Response |
|--------|-----------|----------|
| `400` | Missing/blank field, non-US location, fuel out-of-range | `{"detail": "..."}` or field errors |
| `404` | Valid route but no feasible fuel-stop sequence exists | `{"detail": "No feasible fuel-stop plan is available for this route."}` |
| `502` | Geocoding/routing provider timeout or failure | `{"detail": "Unable to retrieve route data from an upstream provider."}` |

---

### `curl` examples

```bash
# New York → Chicago (multi-stop)
curl -X POST http://localhost:8000/api/v1/routes/optimize/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Chicago, IL"}'

# Los Angeles → Houston (long-haul)
curl -X POST http://localhost:8000/api/v1/routes/optimize/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Los Angeles, CA", "finish": "Houston, TX"}'

# With low starting fuel (forces early stop)
curl -X POST http://localhost:8000/api/v1/routes/optimize/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Chicago, IL", "initial_fuel_gallons": 10}'

# Non-US location → 400
curl -X POST http://localhost:8000/api/v1/routes/optimize/ \
  -H "Content-Type: application/json" \
  -d '{"start": "London, UK", "finish": "Chicago, IL"}'

# Missing field → 400
curl -X POST http://localhost:8000/api/v1/routes/optimize/ \
  -H "Content-Type: application/json" \
  -d '{"finish": "Chicago, IL"}'
```

---

## Environment variables

Copy `.env.example` and adjust as needed. All settings have safe defaults.

| Variable                          | Default                                    | Description |
|-----------------------------------|--------------------------------------------|-------------|
| `DEBUG`                           | `true`                                     | Django debug mode |
| `DJANGO_SECRET_KEY`               | *(insecure dev default)*                   | Replace in production |
| `ALLOWED_HOSTS`                   | `localhost,127.0.0.1`                      | Comma-separated host list |
| `OSRM_BASE_URL`                   | `https://router.project-osrm.org`         | OSRM routing endpoint |
| `NOMINATIM_BASE_URL`              | `https://nominatim.openstreetmap.org`     | Nominatim geocoding endpoint |
| `GEOCODER_USER_AGENT`             | `fuel-route-optimizer-exercise/...`        | Required by Nominatim ToS |
| `PROVIDER_CONNECT_TIMEOUT_SECONDS`| `2`                                        | HTTP connect timeout |
| `PROVIDER_READ_TIMEOUT_SECONDS`   | `8`                                        | HTTP read timeout |
| `GEOCODE_CACHE_TTL_HOURS`         | `720` (30 days)                            | Geocode cache lifetime |
| `ROUTE_CACHE_TTL_HOURS`           | `168` (7 days)                             | Route cache lifetime |
| `ROUTE_CORRIDOR_MILES`            | `10`                                       | Station corridor half-width |
| `MAX_STATION_DETOUR_MILES`        | `20`                                       | Max roundtrip detour to be eligible |
| `DETOUR_PENALTY_USD_PER_MILE`     | `0.20`                                     | Penalty per mile of detour |
| `STATION_GEOCODE_DELAY_SECONDS`   | `1`                                        | Delay between Nominatim requests during import |

---

## Performance benchmarks

Measured on a mid-range developer laptop (Python 3.12, SQLite, 6,614 geocoded stations).

| Scenario                     | Route              | Candidates | Wall time |
|------------------------------|--------------------|-----------|-----------|
| **Cold** (no DB cache)       | NY → Chicago       | 224       | ~2.2 s   |
| **Warm** (geocodes + route cached) | NY → Chicago | 224     | ~430 ms  |
| Warm, optimizer only         | NY → Chicago       | 224       | ~340 ms  |
| Warm, optimizer only         | NYC → LA           | 270       | ~390 ms  |

**Cold breakdown:**  
- Nominatim geocode ×2: ~150 ms (network, 1 req/sec rate)
- OSRM route (12,659-point geometry): ~850 ms (network)
- Local station query + NumPy spatial index build: ~30 ms
- Optimizer (forward DP, 224 candidates): ~20 ms

**Provider call compliance:**

| Request | Geocode calls | Route calls |
|---------|--------------|-------------|
| Cold    | 2            | 1           |
| Warm    | 0            | 0           |
| Per-station during optimize | 0 | 0 |

---

## Optimization model

### Candidate selection

1. OSRM returns the full-detail GeoJSON `LineString` geometry.
2. A **bounding-box pre-filter** reduces 6,614 stations to ~100–500 within the route envelope.
3. A **NumPy vectorised nearest-segment index** finds each station's closest route point and computes its round-trip detour distance in ~0.5 ms per station (vs ~74 ms naive).
4. Stations with detour > `MAX_STATION_DETOUR_MILES` are discarded.
5. Remaining candidates are sorted by route-mile then price.

### Buy-quantity heuristic ("fill-or-coast")

At each candidate station the optimizer decides how many gallons to buy:

- If the current station price ≤ the cheapest reachable future station's price → **fill the tank completely**
- Otherwise → buy **exactly enough fuel to coast to the nearest cheaper station ahead**

This heuristic is provably optimal when future prices are known (which they are — the entire CSV is loaded), and is O(1) per edge after a one-time O(n²) precomputation of `first_cheaper[i]`.

### Forward DP solver

The candidates form a DAG. The DP computes in O(n²):

```
dp_cost[j]  = minimum total trip cost to arrive at station j
dp_fuel[j]  = fuel remaining after purchasing at j
dp_prev[j]  = predecessor for path reconstruction
```

Edge weight = detour penalty ($/mile × detour_miles) + purchase cost at j.

### Detour cost

Detour distance is the **estimated round-trip** from the nearest route point to the station:

```
detour_miles = 2 × point_to_nearest_route_segment_distance
```

This is an approximation; actual road detour may differ. A configurable monetary penalty (`DETOUR_PENALTY_USD_PER_MILE`) discourages choosing cheap but far-off-route stations.

### Fuel accounting

- `total_fuel_consumed = trip_distance_miles / 10`
- `total_gallons_purchased` = gallons bought **during the trip** (does not include starting fuel already in tank)
- `total_fuel_cost` = sum of `price × gallons` at each stop

---

## Project layout

```
config/              Django project settings, URLs, WSGI/ASGI
routes/
  management/
    commands/
      import_fuel_prices.py   CSV → FuelStation (idempotent)
      geocode_stations.py     Batch geocode stations (resumable)
      seed_station_coords.py  Utility seeder
  services/
    importer.py     CSV parsing, normalisation, deduplication
    geocoding.py    Nominatim adapter + GeocodeCache
    routing.py      OSRM adapter + RouteCache  
    geometry.py     Haversine, NumPy segment index, bounding-box filter
    optimizer.py    Forward-DP fuel-stop solver
  tests/
    test_api.py            16 API integration tests (all mocked)
    test_geometry.py       30 geometry helper tests
    test_geocoding.py       9 geocoding adapter tests
    test_routing.py        12 routing adapter tests
    test_optimizer.py      11 optimizer correctness tests
    test_importer.py       22 CSV import unit tests
    test_import_command.py  7 management command tests
  models.py          FuelStation, GeocodeCache, RouteCache
  serializers.py     DRF input serializer
  views.py           OptimizeRouteView orchestration
  exceptions.py      Domain exceptions + DRF exception handler
postman/
  Fuel-Route-Optimization.postman_collection.json
fuel-prices-for-be-assessment.csv
IMPLEMENTATION_PLAN.md
requirements.txt
.env.example
```

---

## Running tests

```bash
# All 107 tests
python3 -m pytest routes/tests/ -v

# Single module
python3 -m pytest routes/tests/test_optimizer.py -v

# With coverage flag
python3 -m pytest routes/tests/ --tb=short -q
```

No internet access is needed — all external provider calls are mocked.

---

## Linting

```bash
python3 -m ruff check .       # lint
python3 -m ruff check --fix . # auto-fix
```

---

## Postman collection

Import `postman/Fuel-Route-Optimization.postman_collection.json` into Postman. Set environment variable:

```
base_url = http://localhost:8000
```

The collection includes:
- `GET /health/`
- `POST /api/v1/routes/optimize/` — New York → Chicago
- `POST /api/v1/routes/optimize/` — Los Angeles → Houston
- `POST /api/v1/routes/optimize/` — Low initial fuel (10 gal)
- `POST /api/v1/routes/optimize/` — Same city (zero route)
- `POST /api/v1/routes/optimize/` — 400: missing field
- `POST /api/v1/routes/optimize/` — 400: non-US location
- `POST /api/v1/routes/optimize/` — 400: fuel out of range

---

## Known limitations & assumptions

1. **Detour is estimated, not road-routed.** The detour distance uses the straight-line offset from the nearest route point, multiplied by 2. Actual road detour may be longer (winding rural roads) or shorter (highway exits).

2. **Station geocoding quality.** 125 of 6,739 stations could not be resolved by the public Nominatim geocoder and are excluded from route optimization. This is a data quality limitation of the free public geocoder.

3. **Buy-quantity precision.** Purchases are rounded to 2 decimal places. Fuel levels may accumulate small floating-point rounding differences (<0.1 gal).

4. **Public provider availability.** Both Nominatim and OSRM are public services without SLA guarantees. Timeouts return a `502` response. Cached results are served indefinitely on repeat requests.

5. **US-only scope.** Both start and finish must resolve to a US location (validated via Nominatim country code).

6. **SQLite concurrency.** This implementation uses SQLite suitable for single-instance development/demonstration. Production deployment would use PostgreSQL + PostGIS.

---

## Delivery checklist

- [x] Django 5.2.17 (latest stable as of implementation date)
- [x] CSV imported idempotently with quality report
- [x] US-only location validation
- [x] Map-ready GeoJSON route geometry returned
- [x] Optimal fuel stops with price-aware DP optimizer
- [x] No fuel leg exceeds 500 miles
- [x] Fuel totals arithmetically consistent
- [x] ≤2 geocode calls + 1 route call on cold; 0 on warm
- [x] Zero per-station external calls in request path
- [x] 107 automated tests, all mocked
- [x] README, `.env.example`, Postman collection ready
- [ ] Loom recording link: *(add after recording)*
