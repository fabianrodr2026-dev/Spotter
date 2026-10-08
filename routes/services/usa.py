from __future__ import annotations

import functools
from pathlib import Path

from django.conf import settings

from routes.validation import Coordinate

from .location_sources import read_json
from .state_boundaries import StateBoundaries

BOUNDARY_SOURCE = (
    "US Census TIGERweb Generalized_ACS2024 State_County MapServer layer 7, "
    "1:500,000 contiguous states and DC"
)
DEFAULT_BOUNDARY_PATH = Path("artifacts/us-states-2024.geojson.gz")


@functools.lru_cache(maxsize=1)
def load_contiguous_boundaries() -> tuple[StateBoundaries, str]:
    path = Path(getattr(settings, "USA_BOUNDARY_PATH", settings.BASE_DIR / DEFAULT_BOUNDARY_PATH))
    if not path.is_absolute():
        path = settings.BASE_DIR / path
    document, digest = read_json(path)
    return StateBoundaries(document), digest


def in_contiguous_usa(point: Coordinate, boundaries: StateBoundaries | None = None) -> bool:
    loaded = boundaries if boundaries is not None else load_contiguous_boundaries()[0]
    return bool(loaded.containing_states(point.longitude, point.latitude))


def geometry_stays_in_contiguous_usa(
    geometry: dict,
    *,
    boundaries: StateBoundaries | None = None,
    sample_stride: int = 25,
) -> bool:
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        return False
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return False
    loaded = boundaries if boundaries is not None else load_contiguous_boundaries()[0]
    stride = max(1, sample_stride)
    indices = list(range(0, len(coordinates), stride))
    if indices[-1] != len(coordinates) - 1:
        indices.append(len(coordinates) - 1)
    for index in indices:
        point = coordinates[index]
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return False
        lon, lat = float(point[0]), float(point[1])
        if not loaded.containing_states(lon, lat):
            return False
    return True
