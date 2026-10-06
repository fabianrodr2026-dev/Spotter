# Spotter route planner

The Section 2 scaffold provides Django configuration, PostGIS setup, input-shape validation, a health endpoint, and a basic interactive map. It does **not** calculate routes or fuel purchases yet. `POST /api/routes/plan/` validates the coordinate request and returns HTTP 501 for a valid request until later implementation steps are complete. The supplied CSV remains unchanged and has not been imported.

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
