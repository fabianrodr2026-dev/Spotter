# Section 4 sources and verification

As of 2026-10-07, the setup pipeline resolves stations offline from cached OSM fuel POIs and Census state polygons. Contested same-brand stores in one city stay in review. Nationwide Overpass tiling is resumable; a complete national snapshot is optional for higher coverage.

## Redistributable cached sources

- `osm-fuel-sample.json`: original Oklahoma-area plus numbered-address search sample (915 POIs).
- `osm-fuel-enriched-sample.json`: prior sample plus targeted city extracts (Pilot/Gila Bend, QuikTrip Henryetta/Amarillo, and nearby fuel nodes). Still a verification extract, not nationwide coverage. Copyright OpenStreetMap contributors, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
- `us-states-2024.geojson.gz`: Census TIGERweb Generalized ACS2024 States 500K. Decompressed SHA-256 `a8cb279aad84eab5aa3eb55e743516deabc3e2905447ff367a61ef33b263f2fa`.
- `geocoding-coverage.json` / `geocoding-review-sample.json`: measured enrichment results for the enriched sample.

Default Overpass endpoint: `https://overpass.openstreetmap.fr/api/interpreter` (configurable). Downloads are setup-only, one process, persistent cooldown, no hidden retries. Empty ocean tiles are allowed; the merged snapshot must be non-empty. Tile grid is 4° contiguous-US cells (105 tiles).

## Matcher rules (v4)

Automatic `resolved` requires an OSM **fuel node** inside the expected state polygon, and one of:

1. Unique name + numbered street address + city match, or
2. Unique OSM brand/name match that includes the station store `#number`, or
3. Unique brand/name in that city **and** the CSV has only one store-numbered station for that brand+city+state.

Highway exits without those proofs stay `review`. Multiple same-brand stations in one city never share a single POI. Way/relation centers and city centers are never used as station coordinates. Unresolved stations are excluded from `usable_stations()`.

## Actual review findings

| Source station | Decision |
| --- | --- |
| `20`, Pilot #1243, Gila Bend AZ, `I-8 EXIT 119` | Resolved to OSM `node/12099611220` (unique Pilot in city; state polygon OK). |
| `71609`, QuikTrip #73, Henryetta OK, highway description | Resolved to `node/11445778092` (unique QuikTrip in Henryetta). |
| `72753` / `72816`, QuikTrip #7908 / #7914, Amarillo TX | Review: `multiple_same_brand_stations_in_city` — do not share one POI. |
| `71246` / `71679`, Toot N Totum stores, Amarillo TX | Review: contested brand+city. |
| `64446`, Speedway #4293, Beloit WI, exit description | Resolved only because brand+city is unique in the CSV and OSM. |

Duplicate brand names in different states (QuikTrip Henryetta OK vs Muskogee OK) resolve to distinct nodes. All eight resolved points pass expected-state and contiguous-US polygon checks.

Enriched-sample run on 6,141 imported stations: **8 resolved / usable**, 4,683 review, 1,448 not-found, 2 unsupported. Second run skips all 6,141 (cache reuse). Counts are not nationwide coverage.

## Remaining environment note

Migration `0004_station_spatial_index` creates the partial GiST index on PostgreSQL/PostGIS. The integration test is skipped under SQLite. Run that test against a PostGIS database when available; it is not required to exercise the offline matcher.
