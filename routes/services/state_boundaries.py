"""Polygon membership for setup-time station checks, using WGS84 GeoJSON."""

import math


CONTIGUOUS_STATES = frozenset(
    "AL AZ AR CA CO CT DE DC FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


def valid_coordinate(longitude: float, latitude: float) -> bool:
    return math.isfinite(longitude) and math.isfinite(latitude) and -180 <= longitude <= 180 and -90 <= latitude <= 90


def ring_contains(ring: list, x: float, y: float) -> bool:
    inside = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if abs(cross) < 1e-12 and min(ax, bx) <= x <= max(ax, bx) and min(ay, by) <= y <= max(ay, by):
            return True
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


class StateBoundaries:
    def __init__(self, document: dict):
        self.states = {}
        self.state_names = {}
        if document.get("type") != "FeatureCollection":
            raise ValueError("State boundaries must be a GeoJSON FeatureCollection")
        for feature in document["features"]:
            state = feature["properties"]["STUSAB"]
            if state not in CONTIGUOUS_STATES:
                continue
            geometry = feature["geometry"]
            if geometry["type"] not in ("Polygon", "MultiPolygon"):
                raise ValueError("State geometry must be polygonal")
            polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            prepared = []
            for polygon in polygons:
                if not polygon:
                    raise ValueError("Empty state polygon")
                for ring in polygon:
                    if len(ring) < 4 or ring[0] != ring[-1] or any(not valid_coordinate(*point) for point in ring):
                        raise ValueError("Invalid state polygon ring")
                xs, ys = zip(*polygon[0])
                prepared.append(((min(xs), min(ys), max(xs), max(ys)), polygon))
            if state in self.states:
                raise ValueError("Duplicate state feature")
            self.states[state] = prepared
            self.state_names[state] = feature["properties"].get("NAME", state).strip().upper()

    def contains(self, state: str, longitude: float, latitude: float) -> bool:
        if not valid_coordinate(longitude, latitude):
            return False
        for (left, bottom, right, top), polygon in self.states.get(state, []):
            if left <= longitude <= right and bottom <= latitude <= top:
                if ring_contains(polygon[0], longitude, latitude) and not any(
                    ring_contains(hole, longitude, latitude) for hole in polygon[1:]
                ):
                    return True
        return False

    def containing_states(self, longitude: float, latitude: float) -> list[str]:
        return [state for state in self.states if self.contains(state, longitude, latitude)]
