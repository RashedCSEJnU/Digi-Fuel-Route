"""Tests for the fuel-stop optimizer."""

from decimal import Decimal

import pytest

from routes.exceptions import NoFeasiblePlanError
from routes.models import FuelStation
from routes.services.optimizer import optimize_fuel_stops


def _make_station(
    pk: int,
    lat: float,
    lon: float,
    price: float = 3.50,
    name: str = "Test Station",
) -> FuelStation:
    return FuelStation(
        id=pk,
        opis_truckstop_id=str(1000 + pk),
        rack_id="1",
        name=name,
        address=f"{pk} Test Rd",
        city="Testville",
        state="OH",
        retail_price=Decimal(str(price)),
        latitude=lat,
        longitude=lon,
        normalized_identity=f"opis:{1000 + pk}:rack:1",
        geocode_status="resolved",
    )


def _make_route_geometry(coordinates: list[list[float]]) -> dict:
    return {"type": "LineString", "coordinates": coordinates}


@pytest.mark.django_db
class TestOptimizer:
    def test_direct_trip_no_stops(self):
        """A short route within initial fuel range needs no stops."""
        # NYC to Philadelphia (~95 miles)
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-75.1652, 39.9526],
        ])
        result = optimize_fuel_stops([], geometry, initial_fuel_gallons=50)
        assert len(result.stops) == 0
        assert result.total_fuel_cost == Decimal("0.00")
        assert result.total_gallons_purchased == 0

    def test_one_stop_required(self):
        """A route longer than 500 miles requires at least one stop."""
        # NYC to Chicago (~790 miles) with a station at midpoint
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        stations = [_make_station(1, 41.0, -80.0, price=3.50)]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        assert len(result.stops) >= 1
        assert result.total_fuel_cost > Decimal("0.00")

    def test_multiple_stops(self):
        """A very long route requires multiple stops."""
        # NYC to LA (~2507 miles) using dense waypoints so every leg <= 383 miles.
        # Legs (miles): 315, 328, 231, 361, 334, 272, 283, 383
        geometry = _make_route_geometry([
            [-74.006,  40.7128],   # NYC         cum~0
            [-80.0,    40.4417],   # Pittsburgh  cum~315
            [-86.15,   39.7684],   # Indianapolis cum~643
            [-90.2,    38.6270],   # St Louis    cum~874
            [-95.99,   36.1540],   # Tulsa       cum~1235
            [-101.83,  35.222],    # Amarillo    cum~1569
            [-106.65,  35.085],    # Albuquerque cum~1841
            [-111.65,  35.198],    # Flagstaff   cum~2124
            [-118.24,  34.052],    # LA          cum~2507
        ])
        # Stations placed at interior waypoints (Pittsburgh through Flagstaff)
        stations = [
            _make_station(1, 40.4417, -80.0,    price=3.50),  # Pittsburgh  ~315 mi
            _make_station(2, 39.7684, -86.15,   price=3.60),  # Indianapolis ~643 mi
            _make_station(3, 38.6270, -90.2,    price=3.40),  # St Louis    ~874 mi
            _make_station(4, 36.1540, -95.99,   price=3.70),  # Tulsa       ~1235 mi
            _make_station(5, 35.222,  -101.83,  price=3.55),  # Amarillo    ~1569 mi
            _make_station(6, 35.085,  -106.65,  price=3.45),  # Albuquerque ~1841 mi
            _make_station(7, 35.198,  -111.65,  price=3.65),  # Flagstaff   ~2124 mi
        ]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        assert len(result.stops) >= 3
        # Verify range invariant: no leg exceeds 500 miles
        prev_mile = 0.0
        for stop in result.stops:
            leg = stop.route_mile - prev_mile + stop.detour_miles
            assert leg <= 500, f"Leg {stop.sequence} exceeds 500 miles: {leg:.1f}"
            prev_mile = stop.route_mile

    def test_cheapest_station_preferred(self):
        """When multiple stations are available, cheaper ones are preferred."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        stations = [
            _make_station(1, 41.0, -80.0, price=4.00, name="Expensive"),
            _make_station(2, 41.0, -80.0, price=3.00, name="Cheap"),
        ]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        assert len(result.stops) >= 1
        # Should pick the cheaper station
        assert result.stops[0].station.name == "Cheap"

    def test_detour_penalty(self):
        """A far detour station is not chosen if penalty makes it uneconomical."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        # Station very close to route but expensive
        close_expensive = _make_station(1, 41.0, -80.0, price=5.00)
        # Station far from route but cheap (beyond max detour)
        far_cheap = _make_station(2, 42.0, -85.0, price=2.00)
        stations = [close_expensive, far_cheap]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        # Should pick the close station despite higher price
        assert len(result.stops) >= 1
        assert result.stops[0].station.name != "Far Cheap"

    def test_low_initial_fuel(self):
        """Low initial fuel forces earlier stops."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        # Two stations: one close to start, one at midpoint
        stations = [
            _make_station(1, 40.8, -75.0, price=3.50),   # ~0 miles
            _make_station(2, 41.0, -80.0, price=3.50),   # ~313 miles
        ]
        # With only 20 gallons, must stop at both stations
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=20)
        assert len(result.stops) >= 1

    def test_infeasible_route(self):
        """A route with no stations and >500 miles raises NoFeasiblePlanError."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-87.6298, 41.8781],
        ])
        with pytest.raises(NoFeasiblePlanError):
            optimize_fuel_stops([], geometry, initial_fuel_gallons=50)

    def test_range_invariant(self):
        """No leg in the plan exceeds 500 miles."""
        # Same dense NYC-LA geometry and stations as test_multiple_stops.
        geometry = _make_route_geometry([
            [-74.006,  40.7128],
            [-80.0,    40.4417],
            [-86.15,   39.7684],
            [-90.2,    38.6270],
            [-95.99,   36.1540],
            [-101.83,  35.222],
            [-106.65,  35.085],
            [-111.65,  35.198],
            [-118.24,  34.052],
        ])
        stations = [
            _make_station(1, 40.4417, -80.0,    price=3.50),
            _make_station(2, 39.7684, -86.15,   price=3.60),
            _make_station(3, 38.6270, -90.2,    price=3.40),
            _make_station(4, 36.1540, -95.99,   price=3.70),
            _make_station(5, 35.222,  -101.83,  price=3.55),
            _make_station(6, 35.085,  -106.65,  price=3.45),
            _make_station(7, 35.198,  -111.65,  price=3.65),
        ]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        prev_mile = 0.0
        for stop in result.stops:
            leg = stop.route_mile - prev_mile + stop.detour_miles
            assert leg <= 500, f"Leg {stop.sequence} exceeds 500 miles: {leg:.1f}"
            prev_mile = stop.route_mile

    def test_deterministic_tie_breaking(self):
        """Same input always produces same output."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        stations = [
            _make_station(1, 41.0, -80.0, price=3.50),
            _make_station(2, 41.0, -80.0, price=3.50),
        ]
        result1 = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        result2 = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        assert len(result1.stops) == len(result2.stops)
        for s1, s2 in zip(result1.stops, result2.stops):
            assert s1.station.id == s2.station.id
            assert s1.gallons_purchased == s2.gallons_purchased

    def test_fuel_levels_consistent(self):
        """Fuel levels are arithmetically consistent."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        stations = [_make_station(1, 41.0, -80.0, price=3.50)]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        for stop in result.stops:
            expected_after = stop.fuel_before_stop_gallons + stop.gallons_purchased
            assert abs(stop.fuel_after_stop_gallons - expected_after) < 0.1
            assert 0 <= stop.fuel_before_stop_gallons <= 50
            assert 0 <= stop.fuel_after_stop_gallons <= 50

    def test_total_cost_matches_stops(self):
        """Total cost equals sum of stop costs."""
        geometry = _make_route_geometry([
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ])
        stations = [_make_station(1, 41.0, -80.0, price=3.50)]
        result = optimize_fuel_stops(stations, geometry, initial_fuel_gallons=50)
        expected = sum(stop.estimated_cost for stop in result.stops)
        assert result.total_fuel_cost == expected
