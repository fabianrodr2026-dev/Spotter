from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('routes', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='StationGeocodeCache',
            fields=[
                ('key', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('result', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='fuelstation',
            name='geocoding_details',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='fuelstation',
            name='geocoding_key',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
    ]
