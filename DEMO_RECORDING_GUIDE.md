# Loom Video Demonstration Recording Guide

A comprehensive, minute-by-minute guide for recording the ≤ 5-minute technical demonstration video for the **Digi-Fuel-Route** API assignment.

---

## 1. Overview & Objective

This video walkthrough validates the final requirement of the project:
- Present the solution architecture, data pipeline, and vehicle assumptions.
- Demonstrate live API execution, response payload structure, and caching behavior.
- Prove validation and error-handling capabilities.
- Highlight the algorithmic approach (Forward DP + NumPy vectorised index).
- Showcase test suite coverage (107 passing tests, 0 live network calls).

**Maximum duration:** 5 minutes (strict).

---

## 2. Pre-Recording Checklist & Environment Setup

Complete these steps **before** hitting record:

### A. Start the Local API Server
Open a terminal and start the Django development server:
```bash
cd /home/rashed/Desktop/Digi-Fuel-Route
python3 manage.py runserver
```
Verify it responds by visiting `http://localhost:8000/health/` (should return `{"status": "ok"}`).

### B. Configure Postman
1. Open **Postman**.
2. Click **Import** (top left) and select:
   `postman/Fuel-Route-Optimization.postman_collection.json`
3. In the imported collection (`Fuel Route Optimization`):
   - Navigate to the **Variables** tab.
   - Verify `base_url` is set to `http://localhost:8000`.
4. Ensure the following requests are visible in the collection sidebar:
   - `GET /health/`
   - `POST /optimize/ — New York to Chicago (cold cache)`
   - `POST /optimize/ — New York to Chicago (warm cache)`
   - `POST /optimize/ — Los Angeles to Houston`
   - `POST /optimize/ — Low initial fuel (10 gal)`
   - `POST /optimize/ — 400: non-US location`

### C. Prepare Terminal & Code Editor Tabs
Have your browser/Postman and IDE open. Recommended layout:
- **Left half of screen:** Postman
- **Right half of screen:** VS Code / Terminal open to `/home/rashed/Desktop/Digi-Fuel-Route`

In your IDE, open these key files in tabs:
- `routes/services/optimizer.py` (line 74: `optimize_fuel_stops`)
- `routes/services/geometry.py` (line 62: `build_segment_index`)
- `README.md`

---

## 3. Timestamped Demonstration Script (5:00 Max)

Follow this exact schedule during your recording:

```
[0:00 - 0:25] Project Introduction & Vehicle Constraints
[0:25 - 1:10] Data Pipeline, Persistence & Architecture
[1:10 - 2:30] Live Route Optimization & Response Breakdown (Cold)
[2:30 - 3:15] Caching & Performance Verification (Warm)
[3:15 - 4:00] Input Validation & Error Handling
[4:00 - 4:40] Algorithmic Core: Vectorised Index & DP Solver
[4:40 - 5:00] Test Suite Execution & Wrap-Up
```

---

### Phase 1: Introduction & Vehicle Constraints [0:00 – 0:25]

- **Screen to show:** `README.md` or top of project.
- **Action:** Point cursor at the **Fixed vehicle assumptions** table.
- **Speaking Script:**
  > *"Hi everyone, this is the demonstration for the Digi-Fuel-Route optimization API. The goal is to calculate the most cost-effective driving route and fuel-purchase plan for any trip across the continental United States.*
  >
  > *We operate under four fixed vehicle constraints:*
  > *1. Maximum range per full tank: **500 miles**,*
  > *2. Tank capacity: **50 gallons**,*
  > *3. Fuel economy: **10 miles per gallon**,*
  > *4. Starting fuel: **50 gallons (full tank)** by default, using real pricing data from OPIS truck stops."*

---

### Phase 2: Data Pipeline & Architecture [0:25 – 1:10]

- **Screen to show:** File tree showing `fuel-prices-for-be-assessment.csv` and `routes/services/importer.py`.
- **Action:** Briefly show the CSV and explain import behavior.
- **Speaking Script:**
  > *"Our dataset contains over 8,100 raw truck stop records. We built an idempotent CSV import management command that handles UTF-8 BOM encoding, normalizes whitespace and state codes, and deduplicates aliases by OPIS ID and Rack ID, retaining the cheapest valid price.*
  >
  > *Crucially for performance: **station geocoding happens entirely offline at import time**. All resolved coordinates are persisted in SQLite. Zero geocoding or routing calls to external providers ever happen per station during API requests."*

---

### Phase 3: Live Route Optimization — Cold Request [1:10 – 2:30]

- **Screen to show:** Postman → `POST /optimize/ — New York to Chicago (cold cache)`.
- **Action:**
  1. Show the JSON body:
     ```json
     {
       "start": "New York, NY",
       "finish": "Chicago, IL"
     }
     ```
  2. Click **Send**.
  3. Scroll through the response JSON and highlight the 4 main sections.
- **Speaking Script:**
  > *"Let's test a ~790-mile trip from New York to Chicago. Because this is uncached, the system makes two geocoding calls via Nominatim and one routing call via OSRM.*
  >
  > *Looking at the response payload:*
  > *1. **route**: Contains the total road distance (790.6 miles), duration, and a complete GeoJSON `LineString` with over 12,000 coordinate pairs ready for map rendering.*
  > *2. **fuel_stops**: Our optimizer selected 4 strategic stops. Notice that for each stop we provide the station name, exact coordinates, retail price, gallons purchased, and estimated spend.*
  > *3. **summary**: Displays total fuel consumed (79.87 gallons), total gallons purchased during the trip (29.95 gallons), and total out-of-pocket spend ($101.42). Note that fuel already in the starting tank is not charged to the trip.*
  > *4. **metadata**: Confirms `cached_route: false` and shows 224 candidate stations were evaluated along the corridor."*

---

### Phase 4: Caching & Performance — Warm Request [2:30 – 3:15]

- **Screen to show:** Postman → `POST /optimize/ — New York to Chicago (warm cache)`.
- **Action:**
  1. Click **Send** again on the same request.
  2. Point out the fast response time (~400 ms) and scroll directly to `metadata`.
- **Speaking Script:**
  > *"Now, let's send the exact same request again.*
  >
  > *Notice the latency drops immediately to approximately 400 milliseconds. If we inspect the metadata at the bottom, `cached_route` is now `true`, and both `cached_geocodes` are `true`.*
  >
  > *This demonstrates our strict provider budget: zero external HTTP calls on warm requests. All candidate filtering and dynamic programming execute locally in-memory."*

---

### Phase 5: Input Validation & Error Handling [3:15 – 4:00]

- **Screen to show:** Postman → `POST /optimize/ — 400: non-US location`.
- **Action:**
  1. Show body with non-US finish:
     ```json
     {
       "start": "New York, NY",
       "finish": "Toronto, Canada"
     }
     ```
  2. Click **Send**. Show the `400 Bad Request` response:
     ```json
     {
       "detail": "Location must be within the United States: 'Toronto, Canada'."
     }
     ```
  3. Change `initial_fuel_gallons` to `-5` or `60` to show range validation error.
- **Speaking Script:**
  > *"Next, let's look at input validation. The specification requires start and finish locations to be strictly within the United States.*
  >
  > *When we pass 'Toronto, Canada', the Nominatim adapter validates the country code and immediately returns a clean 400 Bad Request stating the location must be within the US.*
  >
  > *Similarly, the DRF serializer validates tank capacities, bounds checking initial fuel between 0 and 50 gallons."*

---

### Phase 6: Core Algorithm: Spatial Index & Forward DP [4:00 – 4:40]

- **Screen to show:** IDE with `routes/services/geometry.py` and `routes/services/optimizer.py`.
- **Action:**
  1. Show `geometry.py`: Highlight the NumPy array operations.
  2. Show `optimizer.py`: Highlight the Forward DP DAG traversal.
- **Speaking Script:**
  > *"Under the hood, we solved candidate filtering and route optimization with two high-performance components:*
  >
  > *First, in `geometry.py`, rather than naively checking 8,000 stations against 12,000 route segments in Python loops, we use a NumPy-vectorised segment index with bounding-box pre-filtering. This queries candidates along the corridor in under 50 milliseconds.*
  >
  > *Second, in `optimizer.py`, we implement an O(n²) Forward Dynamic Programming shortest-path algorithm across candidate DAG nodes. It considers detour round-trips, applies a configurable detour penalty ($0.20/mile default), and uses a 'fill-or-coast' heuristic to buy just enough cheaper fuel to reach downstream stations without exceeding the 500-mile tank limit."*

---

### Phase 7: Automated Tests & Wrap-Up [4:40 – 5:00]

- **Screen to show:** Terminal.
- **Action:**
  1. Run the test command:
     ```bash
     pytest
     ```
  2. Let all 107 tests pass green.
- **Speaking Script:**
  > *"Finally, here is our automated test suite. Running pytest executes 107 comprehensive unit and integration tests across API endpoints, geometry, geocoding, routing, and optimization.*
  >
  > *Every external call is mocked using respx and unit mocks—the entire suite runs in under 2.5 seconds with zero internet connection required.*
  >
  > *All requirements are met and ready for production delivery. Thank you!"*

---

## 4. Post-Recording Steps (Finalizing the Delivery)

Once you finish your recording on [Loom](https://www.loom.com):

1. **Copy your Loom sharing URL** (e.g., `https://www.loom.com/share/abcdef123456...`).
2. **Update `README.md`**:
   In `README.md` under `## Delivery checklist` (around line 146):
   ```markdown
   - [x] Loom recording link: https://www.loom.com/share/your-link-here
   ```
3. **Update `IMPLEMENTATION_PLAN.md`**:
   In `IMPLEMENTATION_PLAN.md` under `## 16. Definition of done` (around line 477):
   ```markdown
   - [x] README, .env.example, Postman collection, and a <=5 minute Loom link are present/ready for delivery: https://www.loom.com/share/your-link-here
   ```
4. **Commit and Push**:
   ```bash
   git add README.md IMPLEMENTATION_PLAN.md DEMO_RECORDING_GUIDE.md
   git commit -m "docs: add demonstration video link and recording guide"
   git push origin phase7
   ```

You are now 100% complete with all deliverables!
