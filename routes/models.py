from django.db import models


class FuelPriceDataset(models.Model):
    sha256 = models.CharField(max_length=64, primary_key=True)
    source_filename = models.CharField(max_length=255)
    source_row_count = models.PositiveIntegerField()
    imported_at = models.DateTimeField(auto_now_add=True)


class FuelStation(models.Model):
    dataset = models.ForeignKey(FuelPriceDataset, on_delete=models.CASCADE, related_name="stations")
    source_station_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.CharField(max_length=64)
    price_usd_per_gallon = models.DecimalField(max_digits=24, decimal_places=16)
    price_source_text = models.CharField(max_length=64)
    source_row_numbers = models.JSONField(default=list)
    source_names = models.JSONField(default=list)
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    geocoding_status = models.CharField(max_length=20, default="pending")
    geocoding_source = models.CharField(max_length=100, blank=True)
    geocoding_confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    location_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dataset", "source_station_id"], name="unique_station_per_dataset"),
        ]
        indexes = [models.Index(fields=["dataset", "state", "city"])]


class FuelPriceSourceRow(models.Model):
    dataset = models.ForeignKey(FuelPriceDataset, on_delete=models.CASCADE, related_name="source_rows")
    row_number = models.PositiveIntegerField()
    physical_line_number = models.PositiveIntegerField()
    raw_values = models.JSONField()
    normalized_values = models.JSONField(default=dict)
    status = models.CharField(max_length=16)
    issue = models.CharField(max_length=100, blank=True)
    station = models.ForeignKey(FuelStation, null=True, blank=True, on_delete=models.SET_NULL, related_name="source_rows")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dataset", "row_number"], name="unique_source_row_per_dataset"),
        ]
        indexes = [models.Index(fields=["dataset", "status"])]
