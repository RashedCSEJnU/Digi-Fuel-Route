"""Tests for geometry helpers: haversine, route points, segment index, and filters.

Covers the test matrix entry:
  Geometry: segment accumulation, nearest-mile/projection,
            corridor inclusion/exclusion.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from routes.services.geometry import (
    _SegmentIndex,
    bounding_box_filter,
    build_route_points,
    haversine_miles,
    nearest_route_point,
    point_to_segment_distance_miles,
)

# ---------------------------------------------------------------------------
# haversine_miles
# ---------------------------------------------------------------------------


class TestHaversineMiles:
    def test_same_point_is_zero(self):
        assert haversine_miles(40.7128, -74.006, 40.7128, -74.006) == pytest.approx(0.0, abs=1e-6)

    def test_nyc_to_chicago_approx(self):
        # Straight-line NYC → Chicago is ~711 statute miles.
        d = haversine_miles(40.7128, -74.006, 41.8781, -87.6298)
        assert 700 < d < 720

    def test_symmetry(self):
        d1 = haversine_miles(40.7128, -74.006, 34.0522, -118.2437)
        d2 = haversine_miles(34.0522, -118.2437, 40.7128, -74.006)
        assert d1 == pytest.approx(d2, rel=1e-9)

    def test_accepts_decimal_inputs(self):
        """Decimal model fields must not cause type errors."""
        d = haversine_miles(
            Decimal("40.7128"), Decimal("-74.0060"),
            Decimal("41.8781"), Decimal("-87.6298"),
        )
        assert d > 0

    def test_short_known_distance(self):
        # 1 degree of latitude ≈ 69 miles
        d = haversine_miles(40.0, -90.0, 41.0, -90.0)
        assert 68 < d < 70


# ---------------------------------------------------------------------------
# point_to_segment_distance_miles
# ---------------------------------------------------------------------------


class TestPointToSegment:
    def test_point_on_segment_is_zero(self):
        # Midpoint of segment A→B
        d = point_to_segment_distance_miles(-80.0, 41.0, -81.0, 41.0, -79.0, 41.0)
        assert d == pytest.approx(0.0, abs=0.01)

    def test_point_at_endpoint_a(self):
        d = point_to_segment_distance_miles(-81.0, 41.0, -81.0, 41.0, -79.0, 41.0)
        assert d == pytest.approx(0.0, abs=0.01)

    def test_perpendicular_offset(self):
        # Station 1 degree north of horizontal segment — roughly 69 miles
        d = point_to_segment_distance_miles(-80.0, 42.0, -81.0, 41.0, -79.0, 41.0)
        assert 65 < d < 73

    def test_zero_length_segment(self):
        # Segment of length 0 — should return distance from P to A
        d = point_to_segment_distance_miles(-80.0, 42.0, -80.0, 41.0, -80.0, 41.0)
        assert d > 0


# ---------------------------------------------------------------------------
# build_route_points
# ---------------------------------------------------------------------------


class TestBuildRoutePoints:
    def test_single_coord_returns_empty(self):
        result = build_route_points([[-74.006, 40.7128]])
        assert result == []

    def test_empty_returns_empty(self):
        result = build_route_points([])
        assert result == []

    def test_two_point_route(self):
        # NYC → Chicago (GeoJSON is [lon, lat])
        result = build_route_points([[-74.006, 40.7128], [-87.6298, 41.8781]])
        assert len(result) == 2
        assert result[0].cumulative_miles == pytest.approx(0.0)
        assert result[1].cumulative_miles > 700

    def test_cumulative_distance_accumulates(self):
        coords = [
            [-74.006, 40.7128],
            [-80.0, 41.0],
            [-87.6298, 41.8781],
        ]
        pts = build_route_points(coords)
        assert len(pts) == 3
        assert pts[0].cumulative_miles == pytest.approx(0.0)
        assert pts[1].cumulative_miles > pts[0].cumulative_miles
        assert pts[2].cumulative_miles > pts[1].cumulative_miles

    def test_latitude_longitude_correct(self):
        coords = [[-74.006, 40.7128], [-87.6298, 41.8781]]
        pts = build_route_points(coords)
        assert pts[0].latitude == pytest.approx(40.7128)
        assert pts[0].longitude == pytest.approx(-74.006)
        assert pts[1].latitude == pytest.approx(41.8781)
        assert pts[1].longitude == pytest.approx(-87.6298)

    def test_three_collinear_segments_accumulate_symmetrically(self):
        # Two equal-length segments separated by 1 degree of longitude at lat 40
        coords = [
            [-90.0, 40.0],
            [-91.0, 40.0],
            [-92.0, 40.0],
        ]
        pts = build_route_points(coords)
        seg1 = pts[1].cumulative_miles
        total = pts[2].cumulative_miles
        assert 45 < seg1 < 60  # ~53 miles per degree at lat 40
        assert total == pytest.approx(2 * seg1, rel=0.01)


# ---------------------------------------------------------------------------
# _SegmentIndex (NumPy vectorised nearest-point lookup)
# ---------------------------------------------------------------------------


class TestSegmentIndex:
    """Tests for the vectorised _SegmentIndex used in candidate building."""

    @pytest.fixture
    def three_point_route(self):
        coords = [[-74.006, 40.7128], [-80.0, 41.0], [-87.6298, 41.8781]]
        return build_route_points(coords)

    def test_station_on_route_has_near_zero_offset(self, three_point_route):
        idx = _SegmentIndex(three_point_route)
        _, offset = idx.nearest(41.0, -80.0)
        assert offset < 0.5  # within half a mile

    def test_station_far_away_has_large_offset(self, three_point_route):
        idx = _SegmentIndex(three_point_route)
        # Station ~5 degrees north of route
        _, offset = idx.nearest(46.0, -80.0)
        assert offset > 300

    def test_route_mile_increases_along_route(self, three_point_route):
        idx = _SegmentIndex(three_point_route)
        pt_start, _ = idx.nearest(40.7128, -74.006)
        pt_mid, _ = idx.nearest(41.0, -80.0)
        pt_end, _ = idx.nearest(41.8781, -87.6298)
        assert pt_start.cumulative_miles <= pt_mid.cumulative_miles
        assert pt_mid.cumulative_miles <= pt_end.cumulative_miles

    def test_single_segment_route(self):
        coords = [[-74.006, 40.7128], [-87.6298, 41.8781]]
        pts = build_route_points(coords)
        idx = _SegmentIndex(pts)
        _, offset = idx.nearest(40.7128, -74.006)
        assert offset < 1.0

    def test_decimal_lat_lon_accepted(self, three_point_route):
        """Decimal inputs from Django model fields must not raise."""
        idx = _SegmentIndex(three_point_route)
        _, offset = idx.nearest(Decimal("41.0"), Decimal("-80.0"))
        assert offset < 0.5

    def test_nearest_route_point_without_index(self, three_point_route):
        """nearest_route_point() without pre-built index still works."""
        _pt, offset = nearest_route_point(41.0, -80.0, three_point_route)
        assert offset < 0.5

    def test_nearest_route_point_with_index_matches(self, three_point_route):
        """nearest_route_point() with _index gives same result as without."""
        idx = _SegmentIndex(three_point_route)
        _pt1, off1 = nearest_route_point(41.0, -80.0, three_point_route, _index=idx)
        _pt2, off2 = nearest_route_point(41.0, -80.0, three_point_route)
        # offsets should match within floating-point rounding
        assert off1 == pytest.approx(off2, abs=0.5)


# ---------------------------------------------------------------------------
# bounding_box_filter
# ---------------------------------------------------------------------------


class _MockStation:
    """Lightweight station stub for bounding-box tests (no Django ORM needed)."""

    def __init__(self, lat: float, lon: float):
        self.latitude = Decimal(str(lat))
        self.longitude = Decimal(str(lon))


class TestBoundingBoxFilter:
    @pytest.fixture
    def route_pts(self):
        coords = [[-74.006, 40.7128], [-80.0, 41.0], [-87.6298, 41.8781]]
        return build_route_points(coords)

    def test_station_inside_corridor_included(self, route_pts):
        s = _MockStation(41.0, -80.0)  # on the route
        result = bounding_box_filter([s], route_pts, corridor_miles=10.0)
        assert len(result) == 1

    def test_station_far_north_excluded(self, route_pts):
        s = _MockStation(50.0, -80.0)  # ~620 miles north of route bbox
        result = bounding_box_filter([s], route_pts, corridor_miles=10.0)
        assert len(result) == 0

    def test_empty_station_list(self, route_pts):
        result = bounding_box_filter([], route_pts, corridor_miles=10.0)
        assert result == []

    def test_empty_route_returns_empty(self):
        s = _MockStation(41.0, -80.0)
        result = bounding_box_filter([s], [], corridor_miles=10.0)
        assert result == []

    def test_wider_corridor_includes_more_stations(self, route_pts):
        # A station well outside the tight bounding box
        outer = _MockStation(50.0, -80.0)
        narrow = bounding_box_filter([outer], route_pts, corridor_miles=5.0)
        wide = bounding_box_filter([outer], route_pts, corridor_miles=1000.0)
        assert len(wide) >= len(narrow)

    def test_station_just_inside_eastern_edge(self, route_pts):
        # Within ~5 miles east of the NYC start
        s = _MockStation(40.72, -73.9)
        result = bounding_box_filter([s], route_pts, corridor_miles=10.0)
        assert len(result) == 1

    def test_station_far_east_excluded(self, route_pts):
        s = _MockStation(40.9, -60.0)  # well east of NYC
        result = bounding_box_filter([s], route_pts, corridor_miles=10.0)
        assert len(result) == 0

    def test_multiple_stations_mixed(self, route_pts):
        inside = _MockStation(41.0, -80.0)
        outside = _MockStation(50.0, -120.0)
        result = bounding_box_filter([inside, outside], route_pts, corridor_miles=10.0)
        assert len(result) == 1
        assert result[0] is inside
