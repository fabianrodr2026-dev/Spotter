# Performance measurements (Section 10)

Environment: development machine, mocked routing transport, LocMem plan cache
Samples per cell: 11

Provisional goals (development machine, local processing): under 1000 ms cold local planning; under 300 ms warm cached responses. External provider latency is excluded from these mocked runs and is not guaranteed.

The short-scenario cold p95 includes a one-time contiguous-US boundary file load (~0.5 s) on the first request in the process. After that load, cold medians for all scenarios stay under 10 ms and warm medians under 5 ms on this machine.

| Scenario | Miles | Stops | Cold median (ms) | Cold p95 (ms) | Warm median (ms) | Warm p95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| short | 90.0 | 0 | 6.18 | 545.919 | 3.067 | 5.107 |
| regional | 230.0 | 2 | 8.388 | 8.996 | 4.263 | 4.588 |
| cross_country | 2400.0 | 5 | 6.879 | 7.785 | 2.569 | 3.101 |

Cache keys include endpoints, initial fuel, vehicle assumptions, corridor and snap settings, dataset SHA-256, boundary hash, and routing profile/options/adapter version.

Raw JSON:

```json
{
  "environment": "development machine, mocked routing transport, LocMem plan cache",
  "samples_per_cell": 11,
  "goals": {
    "local_planning_ms": 1000,
    "cached_response_ms": 300
  },
  "scenarios": {
    "short": {
      "distance_miles": 90.0,
      "stop_count": 0,
      "cold_local_ms": {
        "sample_size": 11,
        "median_ms": 6.18,
        "p95_ms": 545.919,
        "min_ms": 4.77,
        "max_ms": 545.919
      },
      "warm_cache_ms": {
        "sample_size": 11,
        "median_ms": 3.067,
        "p95_ms": 5.107,
        "min_ms": 2.487,
        "max_ms": 5.107
      },
      "notes": "Local processing only; provider latency excluded by mocked transport."
    },
    "regional": {
      "distance_miles": 230.0,
      "stop_count": 2,
      "cold_local_ms": {
        "sample_size": 11,
        "median_ms": 8.388,
        "p95_ms": 8.996,
        "min_ms": 7.761,
        "max_ms": 8.996
      },
      "warm_cache_ms": {
        "sample_size": 11,
        "median_ms": 4.263,
        "p95_ms": 4.588,
        "min_ms": 3.661,
        "max_ms": 4.588
      },
      "notes": "Local processing only; provider latency excluded by mocked transport."
    },
    "cross_country": {
      "distance_miles": 2400.0,
      "stop_count": 5,
      "cold_local_ms": {
        "sample_size": 11,
        "median_ms": 6.879,
        "p95_ms": 7.785,
        "min_ms": 5.712,
        "max_ms": 7.785
      },
      "warm_cache_ms": {
        "sample_size": 11,
        "median_ms": 2.569,
        "p95_ms": 3.101,
        "min_ms": 2.421,
        "max_ms": 3.101
      },
      "notes": "Local processing only; provider latency excluded by mocked transport."
    }
  }
}
```
