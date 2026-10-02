"""Fuel-stop optimization engine using dynamic programming."""

import heapq
import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from routes.exceptions import NoFeasiblePlanError
from routes.models import FuelStation
from routes.services.geometry import (
    RoutePoint,
    bounding_box_filter,
    build_route_points,
    nearest_route_point,
)

logger = logging.getLogger("routes.optimizer")

MPG = 10.0
TANK_CAPACITY = 50.0
MAX_RANGE = 500.0
FUEL_DISCRETIZATION = 0.1  # gallons


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

    candidates = []
    for station in filtered:
        if station.retail_price is None or station.retail_price <= 0:
            continue
        if station.latitude is None or station.longitude is None:
            continue

        nearest_point, offset = nearest_route_point(
            station.latitude, station.longitude, route_points
        )
        detour = 2.0 * offset

        if detour > max_detour_miles:
            continue

        candidates.append(
            StationCandidate(
                station=station,
                route_mile=nearest_point.cumulative_miles,
                detour_miles=detour,
                price_per_gallon=station.retail_price,
            )
        )

    candidates.sort(key=lambda c: (c.route_mile, c.price_per_gallon, c.station.id))
    return candidates


def _solve_dp(
    candidates: list[StationCandidate],
    route_distance_miles: float,
    initial_fuel: float,
    detour_penalty_per_mile: float,
) -> list[tuple[int, float]] | None:
    """Solve the fuel-stop problem using Dijkstra on (node, fuel) state space.

    Fuel is discretized to 0.1 gallon units. At each station, we consider
    buying enough to reach any later station or destination.

    Returns a list of (candidate_index, gallons_to_buy) tuples,
    or None if no feasible plan exists.
    """
    n = len(candidates)
    max_fuel_units = int(TANK_CAPACITY / FUEL_DISCRETIZATION)

    # State: (cost, node_index, fuel_units, path)
    # node_index: -1 = origin, 0..n-1 = candidates, n = destination
    # fuel_units: discretized fuel level (0.1 gallon units)

    # visited[(node_index, fuel_units)] = min_cost
    visited: dict[tuple[int, int], float] = {}

    # Priority queue: (cost, node_index, fuel_units, path)
    pq = [(0.0, -1, int(initial_fuel / FUEL_DISCRETIZATION), [])]

    while pq:
        cost, node, fuel_units, path = heapq.heappop(pq)

        # Check if we've reached the destination
        if node == n:
            return path

        # Skip if we've already visited this state with a lower cost
        state_key = (node, fuel_units)
        if state_key in visited and visited[state_key] <= cost:
            continue
        visited[state_key] = cost

        # Explore transitions to all later nodes
        for j in range(node + 1, n + 1):
            if j == n:
                # Destination
                if node == -1:
                    leg_distance = route_distance_miles
                else:
                    leg_distance = route_distance_miles - candidates[node].route_mile
                detour = 0.0
                price = Decimal(0)
            else:
                cand = candidates[j]
                if node == -1:
                    leg_distance = cand.route_mile
                else:
                    leg_distance = cand.route_mile - candidates[node].route_mile
                detour = cand.detour_miles
                price = cand.price_per_gallon

            total_distance = leg_distance + detour
            fuel_needed = total_distance / MPG
            fuel_needed_units = math.ceil(fuel_needed / FUEL_DISCRETIZATION)

            # Check if this leg is feasible
            if fuel_needed > TANK_CAPACITY:
                continue

            if fuel_units < fuel_needed_units:
                continue

            fuel_after_leg_units = fuel_units - fuel_needed_units

            if j == n:
                # Reached destination
                new_cost = cost + detour * detour_penalty_per_mile
                new_path = path
                heapq.heappush(pq, (new_cost, j, fuel_after_leg_units, new_path))
            else:
                # At a candidate station - try different purchase amounts
                cand = candidates[j]

                # Generate purchase options
                buy_options = set()
                buy_options.add(0)  # Buy nothing

                # Enough to reach destination
                remaining_dist = route_distance_miles - cand.route_mile
                remaining_fuel_needed = remaining_dist / MPG
                if remaining_fuel_needed > fuel_after_leg_units * FUEL_DISCRETIZATION:
                    buy_units = math.ceil(
                            (remaining_fuel_needed - fuel_after_leg_units * FUEL_DISCRETIZATION)
                            / FUEL_DISCRETIZATION
                        )
                    buy_options.add(buy_units)

                # Full tank
                buy_options.add(max_fuel_units - fuel_after_leg_units)

                # Enough to reach each later candidate
                for k in range(j + 1, n):
                    later_cand = candidates[k]
                    dist_to_later = (
                        later_cand.route_mile - cand.route_mile + later_cand.detour_miles
                    )
                    fuel_needed_to_later = dist_to_later / MPG
                    if fuel_needed_to_later > TANK_CAPACITY:
                        continue
                    if fuel_needed_to_later > fuel_after_leg_units * FUEL_DISCRETIZATION:
                        buy_units = math.ceil(
                                (fuel_needed_to_later - fuel_after_leg_units * FUEL_DISCRETIZATION)
                                / FUEL_DISCRETIZATION
                            )
                        buy_options.add(buy_units)

                for buy_units in buy_options:
                    if buy_units < 0:
                        continue
                    buy_units = min(buy_units, max_fuel_units - fuel_after_leg_units)
                    if buy_units < 0:
                        continue

                    new_fuel_units = fuel_after_leg_units + buy_units
                    new_cost = (
                        cost
                        + float(price) * buy_units * FUEL_DISCRETIZATION
                        + detour * detour_penalty_per_mile
                    )
                    new_path = path + [(j, buy_units * FUEL_DISCRETIZATION)]
                    heapq.heappush(pq, (new_cost, j, new_fuel_units, new_path))

    return None


def optimize_fuel_stops(
    stations: list[FuelStation],
    route_geometry: dict,
    initial_fuel_gallons: float = TANK_CAPACITY,
) -> OptimizationResult:
    """Find the optimal fuel-stop plan for a route.

    Args:
        stations: All fuel stations in the database
        route_geometry: GeoJSON LineString geometry from OSRM
        initial_fuel_gallons: Starting fuel in gallons (0-50)

    Returns:
        OptimizationResult with selected stops and cost summary

    Raises:
        NoFeasiblePlanError: If no valid plan exists
    """
    coordinates = route_geometry.get("coordinates", [])
    route_points = build_route_points(coordinates)
    if len(route_points) < 2:
        raise NoFeasiblePlanError("Route geometry is invalid")

    route_distance_miles = route_points[-1].cumulative_miles

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
    result.trip_distance_miles = route_distance_miles

    total_detour = sum(candidates[i].detour_miles for i, _ in path)
    result.trip_distance_miles = route_distance_miles + total_detour

    current_fuel = initial_fuel_gallons
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
        current_fuel -= fuel_consumed

        gallons_bought_decimal = Decimal(str(gallons_bought)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        cost = (cand.price_per_gallon * gallons_bought_decimal).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        current_fuel += gallons_bought
        total_cost += cost
        total_purchased += gallons_bought

        result.stops.append(
            FuelStop(
                sequence=seq,
                station=cand.station,
                price_per_gallon=cand.price_per_gallon,
                route_mile=cand.route_mile,
                detour_miles=cand.detour_miles,
                fuel_before_stop_gallons=round(current_fuel - gallons_bought, 2),
                gallons_purchased=float(gallons_bought_decimal),
                fuel_after_stop_gallons=round(current_fuel, 2),
                estimated_cost=cost,
            )
        )

    result.total_fuel_consumed_gallons = round(result.trip_distance_miles / MPG, 2)
    result.total_gallons_purchased = round(total_purchased, 2)
    result.total_fuel_cost = total_cost

    return result
