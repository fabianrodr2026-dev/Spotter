(function () {
  const statusEl = document.getElementById("map-status");
  const summaryEl = document.getElementById("summary");
  const totalsEl = document.getElementById("totals");
  const assumptionsEl = document.getElementById("assumptions");
  const planNode = document.getElementById("plan-data");
  const state = statusEl ? statusEl.dataset.state : "missing";

  const map = L.map("map").setView([39.5, -98.35], 4);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  if (state !== "ready" || !planNode) {
    return;
  }

  let plan;
  try {
    plan = JSON.parse(planNode.textContent);
  } catch (error) {
    statusEl.textContent = "Stored plan data could not be read.";
    return;
  }
  if (!plan || !plan.route || !plan.route.geometry) {
    statusEl.textContent = "Stored plan data is incomplete.";
    return;
  }

  statusEl.textContent = "";
  summaryEl.hidden = false;

  function textItem(label, value) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    totalsEl.appendChild(dt);
    totalsEl.appendChild(dd);
  }

  const totals = plan.totals || {};
  textItem("Distance (miles)", String(plan.route.distance_miles));
  textItem("Duration (seconds)", String(plan.route.duration_seconds));
  textItem("Initial fuel (gal)", String(totals.initial_fuel_gallons));
  textItem("Consumed (gal)", String(totals.fuel_consumed_gallons));
  textItem("Purchased (gal)", String(totals.fuel_purchased_gallons));
  textItem("Remaining (gal)", String(totals.fuel_remaining_gallons));
  textItem("Trip fuel cost (USD)", String(totals.total_fuel_cost_usd));
  textItem("Routing attempts", String(plan.routing_attempts));

  if (Array.isArray(plan.assumptions) && plan.assumptions.length) {
    assumptionsEl.textContent = plan.assumptions.join(" ");
  }

  const layer = L.geoJSON(plan.route.geometry, {
    style: { color: "#0b3d91", weight: 4, opacity: 0.9 },
  }).addTo(map);

  const stops = Array.isArray(plan.stops) ? plan.stops : [];
  stops.forEach(function (stop, index) {
    const marker = L.marker([stop.latitude, stop.longitude]).addTo(map);
    const root = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "Stop " + (index + 1);
    root.appendChild(title);

    const name = document.createElement("div");
    name.textContent = stop.name || stop.source_id || "Station";
    root.appendChild(name);

    const place = document.createElement("div");
    place.textContent = [stop.city, stop.state].filter(Boolean).join(", ");
    if (place.textContent) {
      root.appendChild(place);
    }

    const purchase = document.createElement("div");
    purchase.textContent =
      String(stop.gallons_purchased) +
      " gal @ $" +
      String(stop.price_usd_per_gallon) +
      "/gal = $" +
      String(stop.cost_usd);
    root.appendChild(purchase);
    marker.bindPopup(root);
  });

  const bounds = layer.getBounds();
  if (bounds.isValid()) {
    map.fitBounds(bounds, { padding: [24, 24] });
  }
})();
