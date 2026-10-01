"""Domain exceptions and DRF exception handler for the routes app."""

import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger("routes.exceptions")


class ProviderError(Exception):
    """Raised when an upstream provider (OSRM, Nominatim) fails or returns invalid data."""


class NoRouteError(ProviderError):
    """Raised when OSRM cannot find a route between two points."""


class NonUSLocationError(Exception):
    """Raised when a geocoded location is outside the USA."""


class NoFeasiblePlanError(Exception):
    """Raised when no valid fuel-stop plan exists for the route."""


def drf_exception_handler(exc, context):
    """Map domain exceptions to documented HTTP status codes."""
    if isinstance(exc, NonUSLocationError):
        return Response({"detail": str(exc)}, status=400)
    if isinstance(exc, NoFeasiblePlanError):
        return Response(
            {"detail": "No feasible fuel-stop plan is available for this route."},
            status=404,
        )
    if isinstance(exc, ProviderError):
        return Response(
            {"detail": "Unable to retrieve route data from an upstream provider."},
            status=502,
        )
    # Fall back to DRF's default handler
    return exception_handler(exc, context)
