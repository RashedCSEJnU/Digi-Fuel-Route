"""Fuel-stop optimization engine using forward dynamic programming.

Algorithm overview
------------------
1. Build an ordered list of StationCandidate objects from the route corridor.
2. Represent the problem as a shortest-path on a DAG:
     nodes  = [origin] + sorted_candidates + [destination]
     edges  = every pair (i → j) where the leg fits within tank range
     weight = detour_penalty + gallons_bought × price_per_gallon
3. Run O(n²) forward DP over the node list.

Buy-quantity heuristic (applied per edge, O(1) with precomputed suffix data):
  • If current station price ≤ cheapest price reachable from here on a full tank,
    fill the tank completely.
  • Otherwise buy just enough to reach the nearest cheaper station ahead.
  This is equivalent to the "fill-or-not" greedy known to be optimal when
  future prices are known, and it reduces the number of purchase amounts
  evaluated per edge to O(1).

Complexity: O(n²) where n = corridor candidate count.
With ~250 candidates on a long US route this runs in < 50 ms.
"""

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from routes.exceptions import NoFeasiblePlanError
from routes.models import FuelStation
from routes.services.geometry import (
    RoutePoint,
    _SegmentIndex,
    bounding_box_filter,
    build_route_points,
    nearest_route_point,
)

logger = logging.getLogger("routes.optimizer")

MPG = 10.0
TANK_CAPACITY = 50.0
MAX_RANGE = 500.0

_INF = float("inf")


@dataclass
class StationCandidate:
    """A fuel station candidate along the route."""

    station: FuelStation
    route_mile: float
    detour_miles: float
    price_per_gallon: Decimal


@dataclass
class FuelStop:
    """A selected fuel stop in the optimized plan."""

    sequence: int
    station: FuelStation
    price_per_gallon: Decimal
    route_mile: float
    detour_miles: float
    fuel_before_stop_gallons: float
    gallons_purchased: float
    fuel_after_stop_gallons: float
    estimated_cost: Decimal


@dataclass
class OptimizationResult:
    """The complete optimization result."""

    stops: list[FuelStop] = field(default_factory=list)
    total_fuel_consumed_gallons: float = 0.0
    total_gallons_purchased: float = 0.0
    total_fuel_cost: Decimal = Decimal("0.00")
    trip_distance_miles: float = 0.0
    station_count_considered: int = 0


def _build_candidates(
    stations: list[FuelStation],
    route_points: list[RoutePoint],
    corridor_miles: float,
    max_detour_miles: float,
) -> list[StationCandidate]:
    """Build and filter station candidates along the route."""
    filtered = bounding_box_filter(stations, route_points, corridor_miles)

    # Build segment index once; reuse for all station look-ups.
    seg_index = _SegmentIndex(route_points)

    candidates: list[StationCandidate] = []
    seen_identity: dict[str, int] = {}  # identity → index in candidates list

    for station in filtered:
        if station.retail_price is None or station.retail_price <= 0:
            continue
        if station.latitude is None or station.longitude is None:
            continue

        nearest_point, offset = nearest_route_point(
            station.latitude, station.longitude, route_points, _index=seg_index
        )
        detour = 2.0 * offset

        if detour > max_detour_miles:
            continue

        route_mile = nearest_point.cumulative_miles
        identity = station.normalized_identity or f"id:{station.id}"

        if identity in seen_identity:
            idx = seen_identity[identity]
            existing = candidates[idx]
            if station.retail_price < existing.price_per_gallon:
                candidates[idx] = StationCandidate(
                    station=station,
                    route_mile=route_mile,
                    detour_miles=detour,
                    price_per_gallon=station.retail_price,
                )
        else:
            seen_identity[identity] = len(candidates)
            candidates.append(
                StationCandidate(
                    station=station,
                    route_mile=route_mile,
                    detour_miles=detour,
                    price_per_gallon=station.retail_price,
                )
            )

    candidates.sort(key=lambda c: (c.route_mile, c.price_per_gallon, c.station.id))
    return candidates


def _precompute_suffix_min_price(
    candidates: list[StationCandidate],
) -> list[float]:
    """For each candidate i, compute the minimum price among candidates i..n-1.

    Used in O(1) buy-quantity decisions during DP.
    """
    n = len(candidates)
    suffix = [_INF] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = min(float(candidates[i].price_per_gallon), suffix[i + 1])
    return suffix


def _precompute_reachable_cheaper(
    candidates: list[StationCandidate],
) -> list[int]:
    """For each candidate i, find the index of the first cheaper candidate j > i
    within one full tank range.  Returns n (destination sentinel) if none.
    """
    n = len(candidates)
    result = [n] * n
    prices = [float(c.price_per_gallon) for c in candidates]
    miles = [c.route_mile + c.detour_miles for c in candidates]  # cumulative arrival mile

    for i in range(n):
        p_i = prices[i]
        m_i = candidates[i].route_mile  # departure point (ignoring outbound detour)
        for j in range(i + 1, n):
            leg = candidates[j].route_mile - m_i + candidates[j].detour_miles
            if leg / MPG > TANK_CAPACITY:
                break  # sorted by route_mile; no point looking further
            if prices[j] < p_i:
                result[i] = j
                break
    del miles  # suppress unused warning
    return result


def _solve_dp(
    candidates: list[StationCandidate],
    route_distance_miles: float,
    initial_fuel: float,
    detour_penalty_per_mile: float,
) -> list[tuple[int, float]] | None:
    """Forward DP on the candidate DAG.

    Nodes 0..n-1 = candidates in route order; node n = destination.
    dp_cost[i]        minimum total trip cost to reach node i
    dp_fuel_after[i]  fuel remaining after purchasing at node i
    dp_prev[i]        predecessor node index (-1 = came from origin)

    Returns list of (candidate_index, gallons_bought) pairs, or None.
    """
    n = len(candidates)
    dest = n  # destination sentinel

    dp_cost = [_INF] * (n + 1)
    dp_fuel_after = [0.0] * (n + 1)
    dp_bought = [0.0] * (n + 1)
    dp_prev = [-2] * (n + 1)  # -2 = unset, -1 = came from origin

    prices = [float(c.price_per_gallon) for c in candidates]
    route_miles = [c.route_mile for c in candidates]
    detour_miles = [c.detour_miles for c in candidates]

    # Precompute: for each candidate i, the index of the first cheaper
    # reachable candidate ahead.  Used for O(1) buy quantity decisions.
    first_cheaper = _precompute_reachable_cheaper(candidates)

    def buy_at(j: int, fuel_on_arrival: float, departing_from_mile: float) -> float:
        """How many gallons to buy at candidate j.

        Strategy (provably optimal when future prices are known):
        • If no cheaper station is reachable from j within one tank,
          fill the tank completely.
        • Otherwise, buy exactly enough to reach the cheapest reachable
          cheaper station (the first one, since list is mile-sorted).
        """
        cheaper_k = first_cheaper[j]
        if cheaper_k == dest:
            # No cheaper station ahead in range — fill up
            return TANK_CAPACITY - fuel_on_arrival

        ck = candidates[cheaper_k]
        # Fuel needed to reach ck from j (departing from j, accounting for detour)
        leg = ck.route_mile - route_miles[j] + detour_miles[cheaper_k]
        fuel_needed = leg / MPG
        buy = max(0.0, fuel_needed - fuel_on_arrival)
        # Cap to tank
        return min(buy, TANK_CAPACITY - fuel_on_arrival)

    # ------------------------------------------------------------------ #
    # Phase 1: transitions from ORIGIN (node -1)
    # ------------------------------------------------------------------ #
    for j in range(n + 1):
        if j < n:
            leg_dist = route_miles[j] + detour_miles[j]
        else:
            leg_dist = route_distance_miles

        fuel_needed = leg_dist / MPG
        if fuel_needed > TANK_CAPACITY or fuel_needed > initial_fuel:
            continue

        fuel_arrival = initial_fuel - fuel_needed

        if j == dest:
            dp_cost[j] = 0.0
            dp_fuel_after[j] = fuel_arrival
            dp_bought[j] = 0.0
            dp_prev[j] = -1
        else:
            detour_cost = detour_miles[j] * detour_penalty_per_mile
            gallons = buy_at(j, fuel_arrival, 0.0)
            purchase_cost = prices[j] * gallons
            total_cost = detour_cost + purchase_cost

            if total_cost < dp_cost[j]:
                dp_cost[j] = total_cost
                dp_fuel_after[j] = fuel_arrival + gallons
                dp_bought[j] = gallons
                dp_prev[j] = -1

    # ------------------------------------------------------------------ #
    # Phase 2: transitions from each reached candidate i to later j
    # ------------------------------------------------------------------ #
    for i in range(n):
        if dp_cost[i] == _INF:
            continue

        fuel_leaving = dp_fuel_after[i]
        cost_at_i = dp_cost[i]

        for j in range(i + 1, n + 1):
            if j < n:
                leg_dist = route_miles[j] - route_miles[i] + detour_miles[j]
            else:
                leg_dist = route_distance_miles - route_miles[i]

            # Early-exit: candidates are sorted by mile; beyond max range skip.
            if leg_dist / MPG > TANK_CAPACITY:
                break

            fuel_needed = leg_dist / MPG
            if fuel_needed > fuel_leaving:
                continue

            fuel_arrival = fuel_leaving - fuel_needed

            if j == dest:
                total_cost = cost_at_i
                if total_cost < dp_cost[j]:
                    dp_cost[j] = total_cost
                    dp_fuel_after[j] = fuel_arrival
                    dp_bought[j] = 0.0
                    dp_prev[j] = i
            else:
                detour_cost = detour_miles[j] * detour_penalty_per_mile
                gallons = buy_at(j, fuel_arrival, route_miles[i])
                purchase_cost = prices[j] * gallons
                total_cost = cost_at_i + detour_cost + purchase_cost

                if total_cost < dp_cost[j]:
                    dp_cost[j] = total_cost
                    dp_fuel_after[j] = fuel_arrival + gallons
                    dp_bought[j] = gallons
                    dp_prev[j] = i

    if dp_cost[dest] == _INF:
        return None

    # ------------------------------------------------------------------ #
    # Backtrack from destination to reconstruct the stop sequence
    # ------------------------------------------------------------------ #
    path: list[tuple[int, float]] = []
    node = dest
    while True:
        prev = dp_prev[node]
        if prev == -1 or prev == -2:
            break
        path.append((prev, dp_bought[prev]))
        node = prev

    path.reverse()
    return path


def optimize_fuel_stops(
    stations: list[FuelStation],
    route_geometry: dict,
    initial_fuel_gallons: float = TANK_CAPACITY,
) -> OptimizationResult:
    """Find the optimal fuel-stop plan for a route.

    Args:
        stations: All fuel stations with resolved coordinates
        route_geometry: GeoJSON LineString geometry from OSRM
        initial_fuel_gallons: Starting fuel in gallons (0–50)

    Returns:
        OptimizationResult with selected stops and cost summary

    Raises:
        NoFeasiblePlanError: If no valid plan exists for this route
    """
    coordinates = route_geometry.get("coordinates", [])
    route_points = build_route_points(coordinates)
    if len(route_points) < 2:
        raise NoFeasiblePlanError("Route geometry is invalid")

    route_distance_miles = route_points[-1].cumulative_miles

    # Short route: completes on initial fuel with no stops
    if route_distance_miles / MPG <= initial_fuel_gallons:
        result = OptimizationResult()
        result.trip_distance_miles = route_distance_miles
        result.total_fuel_consumed_gallons = round(route_distance_miles / MPG, 2)
        result.station_count_considered = 0
        return result

    candidates = _build_candidates(
        stations,
        route_points,
        settings.ROUTE_CORRIDOR_MILES,
        settings.MAX_STATION_DETOUR_MILES,
    )

    logger.info(
        "Optimizing route: %.1f miles, %d candidates from %d stations",
        route_distance_miles,
        len(candidates),
        len(stations),
    )

    path = _solve_dp(
        candidates,
        route_distance_miles,
        initial_fuel_gallons,
        settings.DETOUR_PENALTY_USD_PER_MILE,
    )

    if path is None:
        raise NoFeasiblePlanError("No feasible fuel-stop plan is available for this route")

    result = OptimizationResult()
    result.station_count_considered = len(candidates)

    total_detour = sum(candidates[i].detour_miles for i, _ in path)
    result.trip_distance_miles = route_distance_miles + total_detour

    current_fuel = float(initial_fuel_gallons)
    total_cost = Decimal("0.00")
    total_purchased = 0.0

    for seq, (cand_idx, gallons_bought) in enumerate(path, start=1):
        cand = candidates[cand_idx]

        if seq == 1:
            leg_distance = cand.route_mile
        else:
            prev_cand = candidates[path[seq - 2][0]]
            leg_distance = cand.route_mile - prev_cand.route_mile

        leg_distance += cand.detour_miles
        fuel_consumed = leg_distance / MPG
        fuel_before = current_fuel - fuel_consumed

        gallons_bought_dec = Decimal(str(round(gallons_bought, 2)))
        cost = (cand.price_per_gallon * gallons_bought_dec).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        current_fuel = fuel_before + float(gallons_bought_dec)
        total_cost += cost
        total_purchased += float(gallons_bought_dec)

        result.stops.append(
            FuelStop(
                sequence=seq,
                station=cand.station,
                price_per_gallon=cand.price_per_gallon,
                route_mile=cand.route_mile,
                detour_miles=cand.detour_miles,
                fuel_before_stop_gallons=round(fuel_before, 2),
                gallons_purchased=float(gallons_bought_dec),
                fuel_after_stop_gallons=round(current_fuel, 2),
                estimated_cost=cost,
            )
        )

    result.total_fuel_consumed_gallons = round(result.trip_distance_miles / MPG, 2)
    result.total_gallons_purchased = round(total_purchased, 2)
    result.total_fuel_cost = total_cost

    return result
