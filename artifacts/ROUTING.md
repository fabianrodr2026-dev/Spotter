# Routing provider integration (Section 5)

OpenRouteService (HeiGIT) is the selected routing provider. Free Standard
access, distance/waypoint limits, credentials, and usage terms were verified
from public sources on 2026-10-07. The configured API key completed opt-in live
checks. No payment or account creation was performed in this verification.

## Sources checked on 2026-10-07

- [Public restrictions](https://openrouteservice.org/restrictions/): driving
  routes at most 6,000 km and 50 waypoints (including endpoints). Avoid-area
  routes have a 150 km limit, and alternative/round-trip routes a 100 km limit;
  the adapter deliberately does not support those options.
- [Official API migration announcement](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912):
  use `https://api.heigit.org/openrouteservice/v2/directions`; existing API keys
  work at the new endpoint. The adapter sends the key in the Authorization header.
- [Response documentation](https://giscience.github.io/openrouteservice/v9.10.0/api-reference/endpoints/directions/requests-and-return-types):
  GeoJSON LineString, summary distance in meters, duration in seconds, segments,
  and geometry waypoint indices. A live response confirmed that `instructions`
  must be enabled to obtain segments. Instruction text is discarded locally.
- [Current plans](https://account.heigit.org/info/plans) and
  [openrouteservice plans](https://openrouteservice.org/plans/): Standard plan
  is free for everyone. Directions V2 access limits are 2,000 requests per day
  and 40 per minute. The configured key's dashboard screenshot matches those
  Directions quotas.
- [Terms of service](https://account.heigit.org/info/tos) /
  [openrouteservice terms](https://openrouteservice.org/terms-of-service/):
  attribution required as `© openrouteservice by HeiGIT | Data from OpenStreetMap`;
  results are licensed CC-BY-SA 4.0; data quality is not guaranteed and the
  service must not be the sole source for safety-critical navigation; exceeding
  free limits returns errors and repeated violations can temporarily block
  access. Client-side caching of identical requests is compatible with staying
  under quota while retaining attribution/license obligations for displayed
  results.

## Service use

```python
from routes.services.routing import create_routing_session
from routes.validation import Coordinate

session = create_routing_session(
    Coordinate(40.7484, -73.9857), Coordinate(40.7308, -73.9973)
)
initial = session.initial()
# Later sections select and verify station stops using these same session methods:
final = session.through_stops(())
attempts = session.provider.budget.attempts
```

Create one session at the beginning of each planning operation, before local
station search or optimization. Never reset its budget between stages. Instances
are sequential and are not thread-safe. The scaffold API still returns HTTP 501;
Section 9 still exposes the HTTP API/map. Sections 7–8 integrate fuel feasibility
through `RoutePlanner`: `through_stops` returns provider geometry, and the planner
certifies fuel feasibility and station entrance snapping before returning a plan.

The target is initial routing plus routing through all chosen stops in one call.
A single `through_stops(..., repair=True)` is available after the selected-stop
request, including after its failure. Explicit retries of the initial route also
consume the same three-attempt allowance, reducing what remains for later stages.
No automatic retry, redirect following, SDK retry, or HTTP connection retry occurs.
Failures are not cached. A failed connection conservatively counts as an attempt.

The initial route is retained within the session. Successful adapter results are
cached with exact ordered coordinates, profile, accepted options, endpoint/API
version, and adapter schema version. The provider's returned engine version and
attribution are retained. The hosted graph version is unknown before a request;
the configured one-hour TTL bounds reuse across upstream graph changes. The
default Django cache is process-local; use a shared Django backend for multiple
workers. Separate concurrent plans may each incur a miss; there is no distributed
single-flight lock or account-wide quota scheduler.

Settings already exposed in `.env.example` control connection/read timeouts,
the total planning deadline, and cache lifetime. Standard-library HTTPS executes
in a short-lived Python worker; a parent subprocess timeout kills and waits for
the worker when the remaining deadline expires, including during DNS, TLS, or
slow-drip reads. Credentials travel via stdin and the Authorization header, never
process arguments. Responses are limited to 16 MiB. The read timeout is a socket
inactivity timeout; the shared deadline bounds the entire operation. Cache backend
I/O itself must have bounded timeouts when configuring a remote cache.

The adapter checks waypoint count, finite coordinates and the sum of great-circle
leg distances before sending. This lower bound can reject certainly oversized
routes but cannot know the road distance before routing. ORS enforces the actual
distance limit; oversized returned distances are rejected. Default
`avoid_borders=all` prevents international-border crossing; country-polygon input
validation remains a later API concern. Options are limited to `avoid_borders`;
car and HGV profiles are supported without custom vehicle restrictions.

## Verification

```powershell
.\.venv\Scripts\python.exe manage.py test routes.tests.test_routing --settings=config.test_settings
$env:SPOTTER_LIVE_ROUTING = '1'
.\.venv\Scripts\python.exe manage.py test routes.tests.test_routing.LiveRoutingTests --settings=config.test_settings
Remove-Item Env:SPOTTER_LIVE_ROUTING
```

The live tests read `ROUTING_PROVIDER_API_KEY` from the environment or `.env`,
even under test settings (which otherwise use a dummy key). One checks a small
Manhattan route and its cache hit; the other checks the initial route followed
by a route through one waypoint, both returned leg distances, two outbound
attempts, and a cache hit on repetition. They are skipped unless explicitly
enabled. Never commit the key. Mocked tests cover failure responses, limits,
budget exhaustion, deadline expiry, cache identity, and transport-level request
counts.
