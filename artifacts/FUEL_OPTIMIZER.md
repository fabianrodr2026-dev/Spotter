# Fuel optimizer and verified stop routing (Sections 7–8)

## Optimizer

`routes.services.optimizer.optimize_fuel_purchases` is a pure function: no Django
imports and no network access. It takes a fixed ordered stop sequence, per-leg
driving distances (start → stops → destination), and initial fuel.

Vehicle model:

- Tank capacity: 50 US gallons
- Consumption: `leg_miles / 10`
- Full-tank range: 500 miles

Purchase rule at each stop:

1. Find the first later stop within full-tank range that is strictly cheaper.
   Equal prices break ties by `station_id` (lexicographic lower is treated as
   preferred).
2. If such a stop exists, buy only enough fuel to reach it.
3. Otherwise buy the lesser of remaining tank capacity and the fuel needed to
   finish the trip. The destination requires no reserve.

The plan is labeled `is_estimate=True` until Section 8 verifies station access.
Infeasible reachability raises `FuelOptimizationError("infeasible_fuel")`.
Fuel and distance comparisons use explicit tolerances; prices and costs use
`decimal.Decimal`.

## Planner verification

`routes.services.planner.RoutePlanner` uses one `RoutingSession` per request:

1. Initial start→finish route (attempt 1 on cache miss).
2. Corridor candidates → estimate purchases → selected stops with purchases > 0.
3. Second route through those stops; snap each waypoint to the station within
   `STATION_SNAP_TOLERANCE_MILES` (default 0.5); recalculate purchases from the
   returned leg distances (`is_estimate=False`).
4. If snap, routing, or fuel feasibility fails, one bounded local repair replaces
   the failing stop with a nearby corridor alternative and issues the third call.
5. Further failure returns `PlanError("inability_to_plan")`. No unverified
   feasible plan is returned.

Returned `PlannedRoute` includes the final verified geometry, distance, fuel
accounting, selected stops, routing attempt count, and assumptions stating that
purchases are optimized for the final ordered stops while stop selection remains
a corridor heuristic.

## Verification

```powershell
.\.venv\Scripts\python.exe manage.py test routes.tests.test_optimizer routes.tests.test_planner --settings=config.test_settings
```
