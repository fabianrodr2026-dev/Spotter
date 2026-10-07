# Station corridor search (Section 6)

Candidate stations are selected from `usable_stations()` inside a configurable
metric corridor around the provider route geometry, then ordered by cumulative
driving progress from departure.

## Behavior

- Corridor width defaults to `STATION_CORRIDOR_MILES` (5). Distance is geodesic
  offset to the route LineString, not road-network access distance.
- On PostgreSQL/PostGIS, membership uses `ST_DWithin` on the same geography
  expression and quality predicate as the partial GiST index
  `fuelstation_valid_location_gist`. SQLite tests project and filter in Python.
- Cumulative `route_distance_miles` scales geometry segments to the provider's
  driving-distance totals and per-leg distances when waypoint indices are
  present. Straight-line segment length is only a shape measure for that scaling.
- Loops choose the nearest occurrence by offset, then the earlier progress when
  offsets tie.
- `estimated_detour_miles` is `2 * offset_miles` for screening only. It is not a
  verified driving detour; Section 8 must confirm access with a routing call.
- An empty candidate list means insufficient corridor coverage. Narrowing the
  corridor must not invent stations or imply a feasible fuel plan.

```python
from routes.services.station_search import (
    CorridorStationLookup, corridor_has_candidates, find_candidate_stations,
)

candidates = find_candidate_stations(route, corridor_miles=5)
if not corridor_has_candidates(candidates):
    # planner must report insufficient coverage; do not return a success plan
    ...
```

## Verification

```powershell
.\.venv\Scripts\python.exe manage.py test routes.tests.test_station_search --settings=config.test_settings
```

PostGIS index use is asserted only under PostgreSQL.
