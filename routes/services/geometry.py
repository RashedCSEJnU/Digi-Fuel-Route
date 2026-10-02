"""Geometry helpers for route analysis and station corridor filtering."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points in miles."""
    # Cast to float to handle Django Decimal model fields transparently.
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

    Uses a local equirectangular projection centered on the segment midpoint.
    """
    # Cast all args to float to handle Django Decimal model fields transparently.
    px, py = float(px), float(py)
    ax, ay = float(ax), float(ay)
    bx, by = float(bx), float(by)

    mid_lat = (ay + by) / 2
    cos_lat = math.cos(math.radians(mid_lat))

    # Convert to local Cartesian (miles)
    ax_x, ax_y = ax * cos_lat * 69.0, ay * 69.0
    bx_x, bx_y = bx * cos_lat * 69.0, by * 69.0
    px_x, px_y = px * cos_lat * 69.0, py * 69.0

    dx, dy = bx_x - ax_x, bx_y - ax_y
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq == 0:
        return math.sqrt((px_x - ax_x) ** 2 + (px_y - ax_y) ** 2)

    t = max(0, min(1, ((px_x - ax_x) * dx + (px_y - ax_y) * dy) / seg_len_sq))
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


def nearest_route_point(
    station_lat: float, station_lon: float, route_points: list[RoutePoint]
) -> tuple[RoutePoint, float]:
    """Find the nearest route point to a station and return (point, offset_miles).

    Evaluates distance to each route segment and returns the closer endpoint
    of the closest segment as the representative route point.
    """
    best_point = route_points[0]
    best_dist = float("inf")

    for i in range(len(route_points) - 1):
        a = route_points[i]
        b = route_points[i + 1]
        dist = point_to_segment_distance_miles(
            station_lon, station_lat, a.longitude, a.latitude, b.longitude, b.latitude
        )
        if dist < best_dist:
            best_dist = dist
            # Use the closer endpoint as the route point
            if haversine_miles(station_lat, station_lon, a.latitude, a.longitude) < haversine_miles(
                station_lat, station_lon, b.latitude, b.longitude
            ):
                best_point = a
            else:
                best_point = b

    return best_point, best_dist


def bounding_box_filter(
    stations: Sequence,
    route_points: list[RoutePoint],
    corridor_miles: float,
) -> list:
    """Prefilter stations within an expanded bounding box of the route.

    The bounding box is expanded by the corridor width in all directions.
    A degree of latitude is ~69 miles; longitude degrees vary by latitude,
    so we use a conservative expansion that accounts for the route's
    midpoint latitude.
    """
    if not route_points:
        return []

    lats = [p.latitude for p in route_points]
    lons = [p.longitude for p in route_points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Expand latitude by corridor_miles / 69.0 degrees
    lat_expansion = corridor_miles / 69.0

    # Expand longitude by corridor_miles / (69.0 * cos(mid_lat))
    # Use a conservative cos value to avoid over-narrowing at high latitudes
    mid_lat = (min_lat + max_lat) / 2
    cos_mid = max(math.cos(math.radians(mid_lat)), 0.5)  # clamp to avoid division issues
    lon_expansion = corridor_miles / (69.0 * cos_mid)

    return [
        s
        for s in stations
        if (min_lat - lat_expansion) <= float(s.latitude) <= (max_lat + lat_expansion)
        and (min_lon - lon_expansion) <= float(s.longitude) <= (max_lon + lon_expansion)
    ]
