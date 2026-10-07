from django.db import migrations


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("""
            CREATE INDEX fuelstation_valid_location_gist
            ON routes_fuelstation USING GIST (
                (ST_SetSRID(ST_MakePoint(longitude::double precision,
                                        latitude::double precision), 4326)::geography)
            )
            WHERE geocoding_status = 'resolved'
              AND location_verified_at IS NOT NULL AND geocoding_key <> ''
              AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
        """)


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP INDEX IF EXISTS fuelstation_valid_location_gist")


class Migration(migrations.Migration):
    dependencies = [("routes", "0003_station_geocoding")]
    operations = [migrations.RunPython(create_index, drop_index)]
