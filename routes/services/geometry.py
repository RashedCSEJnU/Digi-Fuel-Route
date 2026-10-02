"""Geometry helpers for route analysis and station corridor filtering.

Performance design
------------------
OSRM ``overview=full`` returns 10 000+ coordinate points for long US routes.
A pure-Python O(segments) scan per station creates O(S × N) evaluations that
dominate request latency (~5 s for 461 stations × 12 659 segments).

Solution: pre-extract route segment arrays as NumPy arrays once per route
and evaluate all segments for a station in a single vectorised pass (<1 ms
per station, <0.5 s total for any realistic US route).

The public API is unchanged; the only new symbol is ``_SegmentIndex`` which
is used by ``optimizer._build_candidates`` to avoid per-call array creation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def point_to_segment_distance_miles(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    """Approximate distance from point P to segment AB in miles.

    Uses a local equirectangular projection centred on the segment midpoint.
    Arguments are (longitude, latitude) pairs in degrees.
    """
    px, py = float(px), float(py)
    ax, ay = float(ax), float(ay)
    bx, by = float(bx), float(by)

    mid_lat = (ay + by) / 2
    cos_lat = math.cos(math.radians(mid_lat))
    scale_lon = cos_lat * 69.0
    scale_lat = 69.0

    ax_x, ax_y = ax * scale_lon, ay * scale_lat
    bx_x, bx_y = bx * scale_lon, by * scale_lat
    px_x, px_y = px * scale_lon, py * scale_lat

    dx, dy = bx_x - ax_x, bx_y - ax_y
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq == 0:
        return math.sqrt((px_x - ax_x) ** 2 + (px_y - ax_y) ** 2)

    t = max(0.0, min(1.0, ((px_x - ax_x) * dx + (px_y - ax_y) * dy) / seg_len_sq))
    proj_x = ax_x + t * dx
    proj_y = ax_y + t * dy
    return math.sqrt((px_x - proj_x) ** 2 + (px_y - proj_y) ** 2)


@dataclass
class RoutePoint:
    """A point on the route with cumulative distance."""

    latitude: float
    longitude: float
    cumulative_miles: float


def build_route_points(coordinates: Sequence[Sequence[float]]) -> list[RoutePoint]:
    """Build cumulative route points from GeoJSON coordinates [lon, lat]."""
    if len(coordinates) < 2:
        return []

    points = [
        RoutePoint(
            latitude=coordinates[0][1],
            longitude=coordinates[0][0],
            cumulative_miles=0.0,
        )
    ]
    cumulative = 0.0

    for i in range(1, len(coordinates)):
        prev = points[-1]
        curr_lat, curr_lon = coordinates[i][1], coordinates[i][0]
        segment_dist = haversine_miles(prev.latitude, prev.longitude, curr_lat, curr_lon)
        cumulative += segment_dist
        points.append(
            RoutePoint(latitude=curr_lat, longitude=curr_lon, cumulative_miles=cumulative)
        )

    return points


class _SegmentIndex:
    """Vectorised segment index for fast nearest-point queries.

    Pre-extracts route segment geometry into NumPy arrays so that the
    distance from a single station to all route segments can be evaluated
    in one vectorised pass (~0.5–2 ms for 12 000 segments).

    Usage::

        idx = _SegmentIndex(route_points)
        pt, offset = idx.nearest(station_lat, station_lon)
    """

    def __init__(self, route_points: list[RoutePoint]) -> None:
        self._points = route_points
        n = len(route_points) - 1  # number of segments

        lats = np.array([p.latitude for p in route_points], dtype=np.float64)
        lons = np.array([p.longitude for p in route_points], dtype=np.float64)
        cum = np.array([p.cumulative_miles for p in route_points], dtype=np.float64)

        # Segment start (A) and end (B) arrays — shape (n,)
        self._a_lat = lats[:-1]
        self._a_lon = lons[:-1]
        self._b_lat = lats[1:]
        self._b_lon = lons[1:]
        self._a_cum = cum[:-1]
        self._b_cum = cum[1:]

        # Pre-compute per-segment projection scale (equirectangular, local cos_lat)
        mid_lat = (self._a_lat + self._b_lat) / 2.0
        self._cos_lat = np.cos(np.radians(mid_lat))  # shape (n,)

        # Cartesian endpoints (miles)
        scale_lon = self._cos_lat * 69.0
        scale_lat = 69.0
        self._ax = self._a_lon * scale_lon
        self._ay = self._a_lat * scale_lat
        self._bx = self._b_lon * scale_lon
        self._by = self._b_lat * scale_lat
        self._dx = self._bx - self._ax
        self._dy = self._by - self._ay
        self._seg_len_sq = self._dx ** 2 + self._dy ** 2

        self._n = n

    def nearest(self, station_lat: float, station_lon: float) -> tuple[RoutePoint, float]:
        """Return (closest_route_point, offset_miles) for the given station."""
        slat = float(station_lat)
        slon = float(station_lon)

        # Use the per-segment local cos_lat for the station's projected position
        px = slon * self._cos_lat * 69.0
        py = slat * 69.0

        qx = px - self._ax
        qy = py - self._ay

        # Clamp t to [0, 1]; handle zero-length segments
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(
                self._seg_len_sq > 0,
                np.clip((qx * self._dx + qy * self._dy) / self._seg_len_sq, 0.0, 1.0),
                0.0,
            )

        proj_x = self._ax + t * self._dx
        proj_y = self._ay + t * self._dy
        dists = np.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)

        best_i = int(np.argmin(dists))
        best_dist = float(dists[best_i])

        # Snap to the closer endpoint (for cumulative_miles accuracy)
        a = self._points[best_i]
        b = self._points[best_i + 1]
        dist_a = haversine_miles(slat, slon, a.latitude, a.longitude)
        dist_b = haversine_miles(slat, slon, b.latitude, b.longitude)
        snap_point = a if dist_a <= dist_b else b

        return snap_point, best_dist


def nearest_route_point(
    station_lat: float,
    station_lon: float,
    route_points: list[RoutePoint],
    _index: _SegmentIndex | None = None,
) -> tuple[RoutePoint, float]:
    """Find the nearest route point to a station.

    If ``_index`` (a pre-built _SegmentIndex) is supplied it is used for a
    fast vectorised lookup; otherwise one is built on the fly (slower for
    repeated calls against the same route).
    """
    if _index is not None:
        return _index.nearest(station_lat, station_lon)

    # Fallback: build a temporary index
    idx = _SegmentIndex(route_points)
    return idx.nearest(station_lat, station_lon)


def bounding_box_filter(
    stations: Sequence,
    route_points: list[RoutePoint],
    corridor_miles: float,
) -> list:
    """Prefilter stations within an expanded bounding box of the route."""
    if not route_points:
        return []

    lats = [p.latitude for p in route_points]
    lons = [p.longitude for p in route_points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    lat_expansion = corridor_miles / 69.0
    mid_lat = (min_lat + max_lat) / 2
    cos_mid = max(math.cos(math.radians(mid_lat)), 0.5)
    lon_expansion = corridor_miles / (69.0 * cos_mid)

    return [
        s
        for s in stations
        if (min_lat - lat_expansion) <= float(s.latitude) <= (max_lat + lat_expansion)
        and (min_lon - lon_expansion) <= float(s.longitude) <= (max_lon + lon_expansion)
    ]
