from django.db import migrations


def enable_postgis(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS postgis")


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.RunPython(enable_postgis, reverse_code=migrations.RunPython.noop)]
