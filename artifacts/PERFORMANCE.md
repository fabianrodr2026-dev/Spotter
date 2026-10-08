# Performance measurements

Real database corridor queries, full recorded road geometry, projection, optimization, serialization and caching are measured. Only provider HTTP is replayed for repeated local samples. Live timings are one observation per scenario, not latency guarantees.

Cold clears route and plan caches; boundary loading occurs once per process. Failed plans are not cached, so warm success latency is unavailable for failures. Targets: local planning below 1,000 ms; cached responses below 300 ms.

The cross-country outcome is reported honestly; incomplete station coverage must never be replaced by invented stops.

```json
{
  "environment": {
    "python": "3.12.8",
    "platform": "Windows-11-10.0.26200-SP0",
    "database": "postgresql",
    "cache": "django.core.cache.backends.locmem.LocMemCache"
  },
  "dataset_sha256": "c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241",
  "stations": 6141,
  "usable_stations": 149,
  "scenarios": {
    "short": {
      "outcome": "success",
      "cold_local": {
        "samples": 11,
        "median_ms": 53.041,
        "p95_ms": 650.816
      },
      "warm_cached": {
        "samples": 11,
        "median_ms": 9.053,
        "p95_ms": 13.31
      },
      "distance_miles": 96.01377952755907,
      "stop_count": 0,
      "candidate_count": 3,
      "live_total_ms": 2149.753,
      "live_provider_ms": 1595.734
    },
    "regional": {
      "outcome": "success",
      "cold_local": {
        "samples": 11,
        "median_ms": 149.179,
        "p95_ms": 228.752
      },
      "warm_cached": {
        "samples": 11,
        "median_ms": 12.585,
        "p95_ms": 25.497
      },
      "distance_miles": 388.5510493716695,
      "stop_count": 0,
      "candidate_count": 4,
      "live_total_ms": 2405.072,
      "live_provider_ms": 2299.781
    },
    "long_multi_stop": {
      "outcome": "success",
      "cold_local": {
        "samples": 11,
        "median_ms": 455.471,
        "p95_ms": 562.463
      },
      "warm_cached": {
        "samples": 11,
        "median_ms": 13.541,
        "p95_ms": 19.406
      },
      "distance_miles": 1303.1982596635646,
      "stop_count": 2,
      "candidate_count": 7,
      "live_total_ms": 7229.719,
      "live_provider_ms": 6652.2
    },
    "cross_country": {
      "outcome": "infeasible_fuel",
      "cold_local": {
        "samples": 11,
        "median_ms": 687.176,
        "p95_ms": 850.895
      },
      "warm_cached": null,
      "distance_miles": null,
      "stop_count": 0,
      "candidate_count": null,
      "live_total_ms": 7594.4,
      "live_provider_ms": 6849.923
    }
  }
}
```
