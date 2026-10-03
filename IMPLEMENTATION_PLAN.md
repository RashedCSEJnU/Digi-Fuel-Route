# Fuel Route Optimization API — Implementation Plan

## 1. Assignment outcome

Build a fast Django REST API for a US road trip that accepts start and finish locations and returns:

- Driving-route distance, duration, and GeoJSON `LineString` geometry for map rendering.
- Cost-effective, feasible truck-stop fuel recommendations from the supplied price dataset.
- One or more stops when necessary for a vehicle limited to **500 miles per full tank**.
- Gallons purchased and estimated spend at each selected stop.
- Total trip fuel consumed and total **out-of-pocket fuel spend during this trip**.

Fixed vehicle assumptions:

| Assumption                   | Value                               |
| ---------------------------- | ----------------------------------- |
| Fuel economy                 | 10 miles per gallon                 |
| Tank capacity                | 50 gallons                          |
| Maximum range on a full tank | 500 miles                           |
| Starting tank                | Full (50 gallons) by default        |
| Fuel pricing source          | `fuel-prices-for-be-assessment.csv` |

The source CSV currently contains 8,151 data rows and these columns: `OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Rack ID`, and `Retail Price`. It contains duplicate aliases for at least some truck stops; importing must consolidate them deterministically.

## 2. Scope, non-goals, and key definitions

### In scope

- A JSON API; no custom browser map UI is required.
- Map-ready route GeoJSON and selected station coordinates, so Postman or any map client can display the route and stop markers.
- US-only start and finish validation.
- Cached public geocoding and routing calls.
- Local station search and optimization after the one route request.
- Management command to import, validate, geocode, and report on fuel-price data.
- Automated tests, setup documentation, Postman collection, and a Loom demonstration script.

### Explicit non-goals for the exercise

- Live fuel-price updates.
- Authentication, billing, multi-tenant quotas, or a web frontend.
- Exact turn-by-turn detours to every station. Detour distance is an explicitly labeled geographic estimate so the routing provider is never called per candidate station.
- Production-scale distributed caching/geospatial infrastructure; the design keeps a clean upgrade path to Redis/PostGIS.

### Cost and distance semantics

- `route.distance_miles` is the provider's origin-to-destination road distance.
- `route.trip_distance_miles` is route distance plus the estimated in/out detours for selected stations; it is the distance used in fuel calculations.
- `summary.total_fuel_consumed_gallons = trip_distance_miles / 10`.
- `summary.total_fuel_cost` is the sum of fuel purchased _during the requested trip_. Fuel already in the starting tank is not charged to this trip.
- The plan may buy less than a full tank at a stop when a later cheaper station is reachable. This is necessary for “cost effective” to be meaningful.
- All calculations retain decimal/full precision internally and round only response fields (money to 2 decimals; miles and gallons to 2–3 decimals).

## 3. Technology decisions

| Concern            | Chosen approach                                                                              | Reason                                                                                      |
| ------------------ | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Framework          | Latest stable Django verified on implementation day + Django REST Framework                  | Assignment requirement; mature validation/testing ecosystem.                                |
| Python             | A Django-supported stable Python version (prefer 3.12+ if available)                         | Current compatibility and maintainability.                                                  |
| Database           | SQLite for exercise                                                                          | Zero setup, durable route/geocode caches and station data.                                  |
| HTTP               | `httpx`                                                                                      | Explicit timeouts, simple mocking, connection pooling.                                      |
| Geocoding          | Nominatim/OpenStreetMap, configurable base URL and contact User-Agent                        | Free/no key for an exercise; only called for uncached user inputs and import-time stations. |
| Routing            | Public OSRM `route/v1/driving`, configurable base URL                                        | Free/no key; one request yields distance, duration, and GeoJSON geometry.                   |
| Geometry           | Pure-Python haversine, point-to-segment projection, bounding boxes                           | Portable with SQLite; adequate for an 8k-station dataset.                                   |
| Configuration      | Environment variables and `.env.example`; never commit actual secrets                        | Portable and safe.                                                                          |
| Tests              | Django test runner + pytest/pytest-django (or Django runner only if minimizing dependencies) | Provider calls can be mocked deterministically.                                             |
| Formatting/linting | Ruff and optionally Black                                                                    | Fast, reproducible quality checks.                                                          |

**Provider policy:** public endpoints are demonstration dependencies, not guaranteed production SLAs. Use an informative Nominatim User-Agent, one-at-a-time/rate-limited import geocoding, bounded retries, and cached responses. The implementation must never geocode a station or route to every candidate station during an API request.

## 4. Target project layout

```text
config/
  settings.py
  urls.py
  asgi.py
  wsgi.py
routes/
  migrations/
  management/commands/
    import_fuel_prices.py
  services/
    geocoding.py          # Nominatim adapter, validation, cache behavior
    routing.py            # OSRM adapter, cache behavior
    geometry.py           # miles, segments, station projection/index helpers
    optimizer.py          # candidate selection and fuel-purchase optimization
    importer.py           # CSV parsing, normalization, deduplication
  tests/
    fixtures/
    test_api.py
    test_geocoding.py
    test_routing.py
    test_importer.py
    test_optimizer.py
  admin.py
  apps.py
  models.py
  serializers.py
  urls.py
  views.py
requirements.txt (or pyproject.toml)
.env.example
README.md
postman/Fuel-Route-Optimization.postman_collection.json
IMPLEMENTATION_PLAN.md
```

## 5. Persistence model and indexes

Create migrations before importing data.

### `FuelStation`

- `opis_truckstop_id`, `rack_id`, `name`, `address`, `city`, `state`, `postal_code` if returned by geocoder.
- `retail_price` as `DecimalField` (never float currency).
- `latitude`, `longitude` as nullable decimals/floats until resolved.
- `normalized_identity` (unique): prefer OPIS ID + rack ID; fall back to normalized address/city/state.
- `geocode_status` (`pending`, `resolved`, `unresolved`, `failed`) and optional non-secret failure reason.
- source-row metadata and timestamps.
- indexes: `state`, `geocode_status`, `(latitude, longitude)`, and identity.

For same physical identity/coordinates, retain the lowest valid retail price and a stable representative name/address. Record duplicate and invalid counts in the import report.

### `GeocodeCache`

- `normalized_query` (unique), latitude/longitude, display name, country code, provider response subset, status, `expires_at`, timestamps.
- Cache US-only successful user-location geocodes and optionally known failures with a short TTL.

### `RouteCache`

- `cache_key` (unique), normalized origin/destination coordinate pair, profile, provider name/version, route distance/duration, GeoJSON geometry, `expires_at`, timestamps.
- Cache the raw validated route payload needed by the optimizer. A cache key is directional: A→B differs from B→A.

## 6. External-call and caching budget

### Cold request

1. Geocode origin if absent/expired: at most one Nominatim request.
2. Geocode finish if absent/expired: at most one Nominatim request.
3. Retrieve one OSRM driving route: exactly one request for a valid uncached pair.
4. Query/filter stations and optimize locally: **zero provider calls**.

### Warm request

- Zero provider calls when both geocodes and the route cache are fresh.
- Response metadata must distinguish `cached_geocodes` and `cached_route`.

Implementation details:

- Normalize location text (trim/collapse whitespace and case-fold) before cache lookup.
- Use a shared `httpx.Client`/connection pooling where appropriate.
- Apply short connect/read timeouts (for example 2 seconds connect, 8 seconds read), limited retries only for transient network/5xx failures, and exponential backoff with a small cap.
- Translate upstream failures to a consistent `502` response without exposing internals.
- Log provider name, latency, status, cache hit/miss, and candidate count; do not log secrets.
- Configure cache TTLs, provider URLs, request timeout, corridor width, max detour, and penalty through environment variables.

## 7. API contract

### Health endpoint

`GET /health/` returns `200` with a small JSON status payload. It must not call external providers.

### Route optimization endpoint

`POST /api/v1/routes/optimize/`

Request:

```json
{
	"start": "New York, NY",
	"finish": "Chicago, IL",
	"initial_fuel_gallons": 50
}
```

Rules:

- `start` and `finish`: required non-blank strings; resolved locations must be in the USA.
- `initial_fuel_gallons`: optional decimal from 0 through 50 inclusive; defaults to 50.
- Coordinate input is deliberately not part of v1; keep callers provider-independent.
- If start and finish resolve to the same point or route is zero-length, return a valid zero/near-zero route with no forced stop.

Success (`200`) shape:

```json
{
	"route": {
		"distance_miles": 790.4,
		"trip_distance_miles": 793.2,
		"duration_seconds": 45600,
		"geometry": {
			"type": "LineString",
			"coordinates": [
				[-74.0, 40.7],
				[-73.9, 40.8]
			]
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
				"id": 123,
				"name": "Example Station",
				"address": "...",
				"city": "...",
				"state": "...",
				"coordinates": { "latitude": 41.0, "longitude": -81.0 }
			},
			"price_per_gallon": 3.25,
			"route_mile": 312.8,
			"detour_miles": 1.4,
			"fuel_before_stop_gallons": 18.72,
			"gallons_purchased": 31.42,
			"fuel_after_stop_gallons": 50.0,
			"estimated_cost": 102.12
		}
	],
	"summary": {
		"total_fuel_consumed_gallons": 79.32,
		"total_gallons_purchased": 29.32,
		"total_fuel_cost": 102.12,
		"currency": "USD"
	},
	"metadata": {
		"geocoder_provider": "Nominatim",
		"route_provider": "OSRM",
		"cached_geocodes": { "start": false, "finish": false },
		"cached_route": false,
		"station_count_considered": 42,
		"optimizer_version": "v1"
	}
}
```

The exact output serializer is the source of truth; tests must assert the public fields and numeric consistency.

### Error behavior

| Status | Situation                                                                        | Stable response                                                          |
| ------ | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 400    | Missing/invalid field, unsupported/non-US/ambiguous location                     | `{"detail": "..."}` or DRF field errors for request validation.          |
| 404    | Route is valid but no locally available station plan can make every leg feasible | `{"detail": "No feasible fuel-stop plan is available for this route."}`  |
| 502    | Geocoding/routing provider timeout, malformed payload, or provider failure       | `{"detail": "Unable to retrieve route data from an upstream provider."}` |
| 429    | Optional local rate limiting, if implemented                                     | `{"detail": "Request rate limit exceeded."}`                             |

## 8. Data import plan

Management command:

```bash
python manage.py import_fuel_prices fuel-prices-for-be-assessment.csv
```

Recommended options: `--dry-run`, `--geocode`, `--resume`, `--report path/to/report.json`, and `--limit N` for safe testing.

1. Use `csv.DictReader` with UTF-8 BOM-safe decoding.
2. Verify all seven expected CSV headers before changing the database.
3. Normalize field whitespace; uppercase state; normalize station/address identity; parse `Retail Price` with `Decimal`.
4. Reject/report empty required fields and missing, nonnumeric, zero, or negative price records.
5. Consolidate aliases/duplicates using OPIS ID + Rack ID where reliable; select the cheapest valid price, then a stable source-row tie breaker.
6. Persist normalized stations first. This makes parsing idempotent and separates it from slow provider work.
7. When `--geocode` is requested, resolve only unique unresolved station addresses. Rate-limit Nominatim conservatively (default one request/second), use the persistent geocode cache, validate returned country code `us`, and resume safely after interruption.
8. Never geocode at API request time. Stations without validated coordinates remain excluded from route candidates and visible in the import report.
9. Emit and optionally persist a report with source, read, accepted, inserted, updated, duplicate, invalid, resolved, unresolved, failed, and skipped-cache counts plus a bounded malformed-row sample.

**Practical demo mitigation:** full geocoding of 8k unique stations can take hours at a respectful public Nominatim rate. Start with `--limit` or a prepared relevant-state subset for the demo only if clearly documented; retain the command/resume capability for complete import. Do not violate Nominatim usage policy by firing concurrent bulk requests.

## 9. Local geometry and optimization design

### Candidate construction

1. OSRM returns `overview=full`, `geometries=geojson`, and `alternatives=false`.
2. Validate that geometry is a `LineString` with at least two `[longitude, latitude]` points.
3. Compute cumulative segment distances using haversine distance and preserve the route-mile marker at each segment boundary.
4. Perform a fast bounding-box prefilter expanded by the configured corridor (default 10 miles), then evaluate exact nearest point-to-segment distance in a local projected approximation.
5. Retain resolved stations inside the corridor and calculate:
    - nearest route mile,
    - one-way offset from route,
    - estimated round-trip detour miles (`2 * offset`), and
    - effective progress/cost values.
6. Drop candidates with detour above configurable maximum (for example 20 miles), invalid price, or progress that cannot be part of any 500-mile-feasible path.
7. Deduplicate candidates at the same station/progress point by retaining the lower effective price, then stable ID.

### Feasibility model

Use the ordered nodes `[origin, candidate stations..., destination]` and include estimated station detours in distance/fuel use. An edge is only valid when it can be covered with at most 50 gallons. Initial availability is `initial_fuel_gallons`; all later purchase decisions must keep fuel in `[0, 50]`.

### v1 algorithm: deterministic, price-aware dynamic programming

Implement an exact-on-a-grid dynamic-programming solver rather than a greedy “always fill up” heuristic:

- Use a small fixed fuel discretization (for example 0.1 gallon) so state is `(ordered node index, fuel units remaining)`.
- At each reachable station state, consider enough purchase quantities to reach any later reachable candidate/destination and, optionally, full tank. This avoids an unnecessarily large action space while retaining cheaper-later-station behavior.
- Transition cost is `gallons purchased * station price + detour_penalty_per_mile * incremental_detour`.
- Select the lowest total cost among destination-reachable states; use deterministic tie breakers: lower detour, fewer stops, earlier station ID.
- Reconstruct stops and calculate exact displayed purchases/fuel levels with Decimal-safe arithmetic; the grid is a solver approximation, and response rounding is declared.
- A station is selected only if the plan never reaches negative fuel and no leg exceeds maximum range.

The `detour_penalty_per_mile` is configurable and expressed as dollars/mile equivalent. Set/document a conservative default (for example $0.20/mile) so a nominally cheap station far from the route does not always win. Include the actual fuel burned by the estimated detour in trip consumption and purchases.

If a route needs fuel but no valid candidate sequence connects origin to destination, return the 404 no-feasible-plan error. For a short route that fits the initial fuel, return an empty stop list and zero trip purchase cost.

### Later upgrade path (not required for v1)

- Replace geographic detour estimation with a provider matrix/table service for only the final few selected candidates, subject to API-call budget.
- Use a continuous-resource shortest-path solver, or a MILP, when scale/precision demands it.
- Move prefiltering to PostGIS/Redis spatial indices if requests become CPU-bound.

## 10. Implementation phases and verification gates

### Phase 0 — Baseline and dependency lock

1. Check Python version and current stable Django release on the day work begins.
2. Create virtual environment/project/app; pin compatible dependencies.
3. Add `.gitignore`, `.env.example`, settings split only if necessary, test/lint configuration, health endpoint, and base URLs.

**Verify:** `python manage.py check`, `python manage.py migrate`, `GET /health/`, and dependency versions recorded.

### Phase 1 — Models and fuel data pipeline

1. Implement models/migrations/admin registration.
2. Implement pure parsing/normalization/deduplication functions with unit fixtures.
3. Implement idempotent `import_fuel_prices` command and JSON report.
4. Add cache-backed station geocoding with rate limit/retry/resume behavior.

**Verify:** dry run against supplied CSV; test import subset; repeated import does not create duplicates; invalid rows are reported; geocoding tests use mocked HTTP only.

### Phase 2 — Provider adapters and persistent cache

1. Implement typed internal service results and domain exceptions.
2. Implement Nominatim query validation, US-country check, cache hit/expiry behavior, timeout/retry behavior.
3. Implement OSRM request construction, payload validation, meters-to-miles conversion, and cache behavior.
4. Add structured logs and configuration validation.

**Verify:** mocked adapter unit tests cover cache hit/miss, malformed provider data, timeout, 5xx retry/exhaustion, and non-US geocode. Confirm exactly one route-client invocation on a cache miss and zero on a hit.

### Phase 3 — Geometry and fuel optimizer

1. Add independently testable geometry helpers.
2. Build and order corridor candidates from station fixtures.
3. Implement the dynamic-programming solver and response-plan reconstruction.
4. Enforce initial fuel, tank capacity, range, detour, and no-feasible-plan rules.

**Verify:** deterministic tests for direct trip/no stop, one stop, multiple stops, cheap-station preference, detour penalty behavior, low initial fuel, duplicate candidate selection, infeasible coverage, and every reconstructed leg <= 500 miles.

### Phase 4 — HTTP API integration

1. Add DRF input serializer and response composition.
2. Keep the view orchestration-only: validate → geocode → route → local optimize → serialize.
3. Map domain errors to documented status codes.
4. Add API tests with all provider calls mocked.

**Verify:** happy-path schema/numeric consistency; field validation; non-US input; provider error; 404 plan failure; cache metadata; no external calls in test suite.

### Phase 5 — Performance, documentation, and delivery package

1. Measure cold and warm request latency with a representative trip and record results in README.
2. Confirm candidate filtering/query count and absence of per-station provider calls through mocks/logs.
3. Add setup/run/import/API/environment/assumption/limitation documentation.
4. Export Postman collection with environment variables and examples.
5. Run migrations, checks, formatter, linter, tests, and a manual local request.
6. Record Loom following the script below and add the link to delivery notes after recording.

**Verify:** all quality commands pass; a second identical request shows cache hits; a reviewer can set up and exercise the API using README alone.

## 11. Required test matrix

| Area               | Required cases                                                                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Request validation | missing/blank locations, out-of-range/non-numeric initial fuel, valid default                                                                   |
| Geocoding          | US success/cache hit, non-US rejection, malformed body, timeout/retry failure                                                                   |
| Routing            | valid GeoJSON/cache hit, OSRM no-route/malformed/timeout                                                                                        |
| CSV                | expected headers, BOM, price parsing, invalid price, state normalization, duplicate alias, idempotency                                          |
| Geometry           | segment accumulation, nearest-mile/projection, corridor inclusion/exclusion, antimeridian not required for US scope                             |
| Optimization       | direct trip, one/multiple stops, range invariant, price-aware purchase, detour tradeoff, low initial fuel, no feasible chain, deterministic tie |
| API                | 200 response shape, 400/404/502 mappings, cache metadata, no live HTTP                                                                          |
| Performance        | one routing call on cold cache, zero on warm cache, zero routing/geocoding calls per station                                                    |

## 12. Configuration contract (`.env.example`)

Document non-secret example values for:

```dotenv
DEBUG=true
DJANGO_SECRET_KEY=replace-for-local-development
ALLOWED_HOSTS=localhost,127.0.0.1
OSRM_BASE_URL=https://router.project-osrm.org
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org
GEOCODER_USER_AGENT=fuel-route-optimizer-exercise/contact@example.com
PROVIDER_CONNECT_TIMEOUT_SECONDS=2
PROVIDER_READ_TIMEOUT_SECONDS=8
GEOCODE_CACHE_TTL_HOURS=720
ROUTE_CACHE_TTL_HOURS=168
ROUTE_CORRIDOR_MILES=10
MAX_STATION_DETOUR_MILES=20
DETOUR_PENALTY_USD_PER_MILE=0.20
STATION_GEOCODE_DELAY_SECONDS=1
```

Never commit a real secret key, private API key, or personal contact address.

## 13. README and delivery artifacts

README must include:

1. Problem statement and fixed assumptions.
2. Architecture diagram/text flow: client → cache/geocoder → OSRM → local station optimizer → response.
3. Prerequisites and exact setup/migration/run commands.
4. Import command, report location, safe geocoding behavior, and expected data-quality caveat.
5. Environment variable table.
6. `curl` request and representative success/error response examples.
7. Provider call budget and cache behavior.
8. Optimization model, detour approximation, cost semantics, and limitations.
9. Test/lint commands and benchmark method/results.
10. Link to Postman collection and Loom after recording.

Deliver:

- Django source, migrations, tests, and supplied CSV retained as input data.
- `README.md` and this maintained plan.
- `.env.example`.
- Postman collection/environment (with no secrets).
- Loom link, maximum five minutes.

## 14. Three-day execution schedule

### Day 1 — Foundation and data

- Bootstrap, configuration, models/migrations, parser/import/report tests.
- Run dry import; implement resumable station-geocode command and document that full public bulk geocoding is rate-limited.
- Deliverable checkpoint: database can hold normalized/deduplicated station prices and coordinate status.

### Day 2 — Route and optimization

- Build cached provider adapters with mocks.
- Implement geometry/corridor candidate logic and dynamic-programming solver.
- Build endpoint/API tests and run representative local requests.
- Deliverable checkpoint: endpoint returns route geometry, feasible stops, and internally consistent costs using only one cold route request.

### Day 3 — Hardening and demo

- Finish errors/logging/performance measurement/documentation/Postman collection.
- Run quality gate; warm cache for the demo; make a short Loom.
- Deliverable checkpoint: reproducible repository and reviewed demonstration package.

## 15. Loom + Postman demonstration script (5 minutes maximum)

1. **0:00–0:25:** State the assignment and fixed vehicle assumptions.
2. **0:25–1:10:** Show project structure, the CSV import command/report, and local station persistence.
3. **1:10–2:30:** Use Postman to call the optimization endpoint for a multi-stop US trip. Point out route GeoJSON, station markers/coordinates, prices, purchases, total consumption, and total trip spend.
4. **2:30–3:15:** Repeat exactly the same request. Show `cached_route: true` and explain that fuel-stop optimization occurs locally without route calls per station.
5. **3:15–4:00:** Send non-US or invalid input and show the error response.
6. **4:00–4:40:** Briefly show `optimizer.py`, provider cache model, and the key range/price tests.
7. **4:40–5:00:** Show passing test command and name limitations: public provider availability, import-time station coordinate quality, and estimated—not turn-by-turn—station detours.

## 16. Definition of done

- [x] Django version is verified/pinned and all project checks/migrations pass.
- [x] The supplied CSV imports idempotently with an actionable quality report.
- [x] Only validated US start/finish locations are accepted.
- [x] The response supplies valid map-ready route geometry and station coordinates.
- [x] Optimization is deterministic, range-feasible, price-aware, and includes detour fuel/cost assumptions.
- [x] No selected/reconstructed leg requires more than the available 500-mile full-tank range.
- [x] Fuel totals, purchases, and monetary totals are arithmetically consistent.
- [x] A cache miss makes at most two geocode calls and one route call; a warm request makes none.
- [x] No per-station routing/geocoding occurs in the request path.
- [x] Automated tests cover the matrix above and never require public services.
- [ ] README, `.env.example`, Postman collection, and a <=5 minute Loom link are present/ready for delivery (Loom video pending recording).
