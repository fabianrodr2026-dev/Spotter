# Spotter route planner

The Section 2 scaffold provides Django configuration, PostGIS setup, input-shape validation, a health endpoint, and a basic interactive map. It does **not** calculate routes or fuel purchases yet. `POST /api/routes/plan/` validates the coordinate request and returns HTTP 501 for a valid request until later implementation steps are complete. Section 3 provides the fuel-price audit and idempotent importer, verified against an isolated SQLite database. The supplied CSV remains unchanged; run the import command below to populate your configured database.

## Local setup (PowerShell)

Install Python 3.12.8 and PostgreSQL with the PostGIS extension. Docker Compose is an optional way to provision that database. Django 6.1.2 supports Python 3.12–3.14. This repository selects 3.12.8 because it is the compatible interpreter available on the development machine. The scaffold uses Django's PostgreSQL backend and enables PostGIS with a migration; later spatial queries will use PostGIS functions through that connection. No local GDAL installation is required for this scaffold.

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with a random `DJANGO_SECRET_KEY`, a local `DB_PASSWORD`, and a valid `ROUTING_PROVIDER_API_KEY`. Other settings can retain the example values. Environment variables override `.env`. `DJANGO_ALLOWED_HOSTS` is comma-separated. The routing connection/read timeout, overall deadline, route/plan cache lifetimes, and station corridor are positive numbers configured through the corresponding variables. `DJANGO_CACHE_BACKEND` and `DJANGO_CACHE_LOCATION` configure the cache; the local-memory default is for development and does not share entries across processes. The corridor defaults to 5 miles and may exclude cheaper or necessary stations farther from the selected route.

Create the `spotter` database and user on your PostgreSQL server, grant that user permission to enable PostGIS, and set `DB_HOST`/`DB_PORT` in `.env` to match the server. Then run:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py test routes
.\.venv\Scripts\python.exe manage.py runserver
```

If using the optional Compose service, run `docker compose up -d db` before the Django commands. Its `postgis/postgis:17-3.5` image uses a named volume and host port 55432 by default. The `routes` initial migration enables the `postgis` extension. For a clean database verification, use a new database, run `migrate`, and query `SELECT PostGIS_Version()`.

Check `GET http://127.0.0.1:8000/health/` for `{"status":"ok"}`. `python manage.py check` reports `routes.E001`, `routes.E002`, or `routes.E003` with a setting name when a required secret, database password, or provider key is missing or left as the example placeholder. The health endpoint reports only that the Django process is responding; it does not claim the provider or database is ready. A basic Leaflet map is at `/map/`; browser tile requests use OpenStreetMap with visible attribution and are separate from routing calls. This development map uses [OSM's tile policy](https://operations.osmfoundation.org/policies/tiles/); use a dedicated provider for heavier deployment traffic.

An example accepted request shape is:

```json
{"start":{"latitude":40.7128,"longitude":-74.0060},"finish":{"latitude":34.0522,"longitude":-118.2437},"initial_fuel_gallons":50}
```

Only finite numeric coordinates and 0–50 gallons of initial fuel pass the scaffold's shape checks. Contiguous-US polygon validation, routing, station selection, fuel optimization, and finished map rendering are planned for later sections. See [CONTRACT.md](CONTRACT.md) for accounting and geographic assumptions. Map tiles, when added, and setup-time station geocoding are separate from per-request routing attempts.

## Fuel-price audit and import

The supplied CSV is read as UTF-8 and identified by the SHA-256 of its original bytes. Retail prices are assumed to be USD per US gallon; the file itself has no unit metadata. The importer keeps every source row with its original values, normalized values, CSV record number, physical line number, status, and dataset hash. It collapses whitespace, uppercases two-letter state/province codes, and retains the raw text. A station ID with one consistent address, city, state, rack ID, and decimal price is imported once; name variants are retained as aliases. If any of those location/rack/price fields conflict for an ID, **all rows for that ID are quarantined** and no station is selected. Invalid rows are rejected with an issue code. The same file hash imports only once.

Audit without database access and write the complete conflict report:

```powershell
.\.venv\Scripts\python.exe manage.py import_fuel_prices --audit-only --report-file fuel-price-audit.json
```

After migrations, import the file:

```powershell
.\.venv\Scripts\python.exe manage.py import_fuel_prices
```

The command accepts an optional CSV path and `--report-file PATH`. Its JSON summary reports imported, collapsed, conflicting, and rejected counts. The source audit found 8,151 data rows, 6,738 IDs, 6,141 importable stations, 86 collapsed rows, 1,924 conflict rows across 597 IDs, and zero rejected rows. These counts reconcile to 8,151. Run the audit command above to generate the full local conflict report; it contains station names, addresses, and prices from the supplied CSV. The source includes Canadian province codes; station geography is validated later before route selection. Prices retain their source text and decimal value without currency rounding during import.

For an isolated import test when PostgreSQL/PostGIS is unavailable, use `--settings=config.test_settings`; setting `SPOTTER_TEST_DB_PATH` to a local `.sqlite3` path makes that test database persistent across commands. This is a verification aid, not the configured production database.

## Station coordinate enrichment

Section 4 supplies a setup-only command, persistent result cache, coverage/review reports, and a PostGIS spatial-index migration verified against a running PostGIS database. Offline matching against the bundled enriched OSM sample resolves two stations with exact OSM store-reference evidence, including a highway-exit station. Same-brand city evidence alone cannot verify a station. This verification sample has only two usable stations out of 6,141 and is insufficient for general route optimization. A full national Overpass tile download can improve candidate coverage but does not by itself verify station identity. See [the source and verification record](artifacts/GEOCODING.md).

The selected location source is OpenStreetMap fuel POIs, obtained as resumable 4° Overpass tiles and matched locally. The default endpoint is `GEOCODING_OVERPASS_URL` (French public Overpass by default). This is a one-time setup workflow, not a public Overpass-backed application service. Use one process on one machine. Completed extracts are retained; successful requests are spaced by at least five seconds, failed requests by at least sixty seconds or the server's longer `Retry-After`. There are no automatic failure retries. Empty ocean tiles are kept; an incomplete/error response is never published as the merged snapshot.

OpenStreetMap data is stored under [ODbL 1.0 with contributor attribution](https://www.openstreetmap.org/copyright). State polygons come from [Census TIGERweb, 2024 vintage, 1:500,000](https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2024/State_County/MapServer/7). Both sources are cached and hashed. The OSM export contains OSM objects, not fuel prices; its attribution and license must accompany redistribution. The original assessment CSV's license is not changed.

After configuring and migrating the database and importing the CSV:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py geocode_stations --download --report-file geocoding-coverage.json --review-report geocoding-review.json --export-osm relevant-osm.json
.\.venv\Scripts\python.exe manage.py geocode_stations --coverage-only
```

Without `--download`, enrichment performs no network requests. `--cache-dir PATH` selects the source cache (default `data/geocoding/`); `--osm-file PATH` and `--boundaries-file PATH` accept existing JSON or gzip-compressed JSON. `--dataset SHA256` is required if multiple CSV versions are imported. `--limit N` processes at most N new/changed stations and skips previously completed work, so repeated limited runs advance. Database changes and cached decisions commit together in batches of 100. A failed batch can be replayed. Result keys include source and boundary hashes, station name/aliases/address/city/state, and the matcher version. Negative results are cached too. Changing the source, station input, or matcher invalidates the prior decision. For a deliberate source refresh, use a new cache directory; normal runs reuse their existing snapshots indefinitely.

Automatic resolution requires an OSM **fuel node** inside the expected contiguous-US state polygon, plus one of: (1) unique name + numbered street address + city, (2) unique brand/name whose OSM tags include the station store `#number`, or (3) unique brand/name in that city when the CSV has only one store-numbered station for that brand+city+state. Conflicting country/state tags prevent acceptance. Multiple same-brand stations in one city are left in `review` so they never share one POI. Highway exits without those proofs stay in review. Way/relation bounding-box centers and city centers are never used as station coordinates. Alaska, Hawaii, territories, and Canadian records are unsupported under the existing contiguous-US contract.

`resolved` means these automated checks passed, not that an entrance or truck access was inspected. OSM supplies no calibrated confidence probability, so confidence remains null; `geocoding_details` retains precision, reasons, candidate tags, OSM identities, source versions, and verification method. `location_verified_at` records automated acceptance time. Ambiguous records need independent location evidence before a future reviewed-location workflow can accept them. Coverage includes per-status and per-state counts, usable and excluded totals, and the source scope. Counts for the sample artifact are **not** nationwide coverage.

Station candidate queries use `usable_stations()` and the same quality predicate as the `fuelstation_valid_location_gist` partial GiST geography-expression index. The expression is `ST_SetSRID(ST_MakePoint(longitude::double precision, latitude::double precision), 4326)::geography`; the index updates automatically with station coordinates/status. SQLite verifies matching/cache behavior but does not create this PostGIS index. Current route requests never invoke this setup command or download sources.

Reproduce the bundled sample offline, without production database credentials:

```powershell
$env:SPOTTER_TEST_DB_PATH = "$PWD\geocoding-check.sqlite3"
.\.venv\Scripts\python.exe manage.py migrate --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py import_fuel_prices --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py geocode_stations --osm-file artifacts/osm-fuel-enriched-sample.json --boundaries-file artifacts/us-states-2024.geojson.gz --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py geocode_stations --osm-file artifacts/osm-fuel-enriched-sample.json --boundaries-file artifacts/us-states-2024.geojson.gz --settings=config.test_settings
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings
```

The second enrichment reuses all 6,141 decisions. With the enriched sample, expect on the order of eight usable stations and contested Amarillo multi-store brands left in review. To verify the production index, run `manage.py test routes.tests.test_geocoding.GeocodingTests.test_postgis_partial_spatial_index_exists_and_is_usable` against configured PostgreSQL/PostGIS with permission to create a test database. That test checks the actual index and query plan; it is explicitly skipped under SQLite.

## Routing integration

Section 5 provides the ORS adapter, cached route results, a shared planning deadline, and a maximum of three outbound attempts per routing session. Public Standard-plan limits are Directions V2 2,000/day and 40/minute; driving routes are limited to 6,000 km and 50 waypoints. Attribution and CC-BY-SA 4.0 obligations are recorded in [routing configuration, provider evidence, limitations, and live checks](artifacts/ROUTING.md). Fuel optimization and the finished API remain later steps.

## Station corridor search

Section 6 projects usable stations onto the provider route inside `STATION_CORRIDOR_MILES`, orders them by cumulative driving progress, and keeps prices plus location-quality metadata. Preliminary detour estimates are `2 ×` corridor offset for screening only and are not verified access distances. An empty corridor result is insufficient coverage, not a feasible plan. See [station search notes](artifacts/STATION_SEARCH.md).

## Fuel optimizer and verified stops

Section 7 implements a pure greedy purchase optimizer (50-gallon tank, 10 mpg) for a fixed ordered station sequence, with decimal cost accounting, explicit tolerances, and estimate labeling until access is verified. Section 8 routes through selected purchase stops, snaps waypoints within `STATION_SNAP_TOLERANCE_MILES`, recalculates purchases from verified leg distances, and allows one bounded repair inside the three-attempt routing budget. See [fuel optimizer notes](artifacts/FUEL_OPTIMIZER.md). The HTTP API remains HTTP 501 until Section 9.
