# Section 4 sources and verification

As of 2026-10-07, the setup pipeline checks stations offline against cached OSM fuel POIs and Census state polygons. Highway-exit stations need direct identity evidence; same-brand city matches stay in review. Nationwide Overpass tiling is resumable; a complete national snapshot is optional for higher candidate coverage.

## Redistributable cached sources

- `osm-fuel-sample.json`: original Oklahoma-area plus numbered-address search sample (915 POIs).
- `osm-fuel-enriched-sample.json`: prior sample plus targeted city extracts (Pilot/Gila Bend, QuikTrip Henryetta/Amarillo, and nearby fuel nodes). Still a verification extract, not nationwide coverage. Copyright OpenStreetMap contributors, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
- `us-states-2024.geojson.gz`: Census TIGERweb Generalized ACS2024 States 500K. Decompressed SHA-256 `a8cb279aad84eab5aa3eb55e743516deabc3e2905447ff367a61ef33b263f2fa`.
- `geocoding-coverage.json` / `geocoding-review-sample.json`: measured enrichment results for the enriched sample.

Default Overpass endpoint: `https://overpass.openstreetmap.fr/api/interpreter` (configurable). Downloads are setup-only, one process, persistent cooldown, no hidden retries. Empty ocean tiles are allowed; the merged snapshot must be non-empty. Tile grid is 4° contiguous-US cells (105 tiles).

## Matcher rules (v6)

Automatic `resolved` requires an OSM **fuel node** inside the expected state polygon, and one of:

1. Unique name + numbered street address + city match, or
2. Unique OSM brand/name match that includes the station store `#number` or whose `ref` tag exactly equals that number.

Highway exits without those proofs stay `review`, even if only one same-brand OSM POI appears in the city. Multiple same-brand stations in one city never share a single POI. Way/relation centers and city centers are never used as station coordinates. Unresolved stations are excluded from `usable_stations()`.

## Actual review findings

| Source station | Decision |
| --- | --- |
| `20`, Pilot #1243, Gila Bend AZ, `I-8 EXIT 119` | Review: OSM `node/12099611220` has the brand and is inside AZ, but no matching store number or exit evidence. |
| `71609`, QuikTrip #73, Henryetta OK, highway description | Review: OSM `node/11445778092` lacks a matching store number or exit evidence. |
| `72753` / `72816`, QuikTrip #7908 / #7914, Amarillo TX | Review: `multiple_same_brand_stations_in_city` — do not share one POI. |
| `71246` / `71679`, Toot N Totum stores, Amarillo TX | Review: contested brand+city. |
| `64446`, Speedway #4293, Beloit WI, exit description | Resolved to `node/9206357192`: OSM `ref=4293`, matching brand/city/state; node lies inside WI. |
| `70025`, Speedway #6245, Carrollton OH, `SR-43` | Resolved to `node/9664821532`: OSM `ref=6245`, matching brand/city/state; node lies inside OH. Another Speedway candidate in Carrollton KY is rejected by the state boundary/tag. |

Duplicate brand names in different states are evaluated separately. The two resolved Speedway nodes have distinct matching store references and pass the expected-state and contiguous-US polygon checks. Pilot and QuikTrip highway candidates remain unverified even though their nodes pass state checks.

Enriched-sample run on 6,141 imported stations: **2 resolved / usable**, 4,689 review, 1,448 not-found, 2 unsupported. Second run skips all 6,141 (cache reuse). Counts are not nationwide coverage. Route optimization must treat this sample as insufficient coverage.

## Spatial-index verification

Migration `0004_station_spatial_index` creates the partial GiST index on PostgreSQL/PostGIS. On 2026-10-07, migrations and all 38 Django tests passed against an isolated `postgis/postgis:17-3.5` database, including the index-definition and indexed `ST_DWithin` query-plan test. The same source file imported and enriched in PostGIS; a second enrichment run skipped all 6,141 stations. SQLite test runs skip this database-specific assertion.
