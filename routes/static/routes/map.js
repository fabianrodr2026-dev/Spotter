(function () {
  const statusEl = document.getElementById("map-status");
  const summaryEl = document.getElementById("summary");
  const totalsEl = document.getElementById("totals");
  const assumptionsEl = document.getElementById("assumptions");
  const planNode = document.getElementById("plan-data");
  const state = statusEl ? statusEl.dataset.state : "missing";

  const map = L.map("map").setView([39.5, -98.35], 4);
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17,
    attribution:
      'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors | DEM: SRTM, Sonny | Map style: &copy; <a href="https://opentopomap.org/about">OpenTopoMap</a> (CC-BY-SA)',
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
  function quantity(value, digits = 3) {
    return Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
  }
  textItem("Distance (miles)", quantity(plan.route.distance_miles, 1));
  textItem("Duration (hours)", quantity(plan.route.duration_seconds / 3600, 1));
  textItem("Initial fuel (gal)", quantity(totals.initial_fuel_gallons));
  textItem("Consumed (gal)", quantity(totals.fuel_consumed_gallons));
  textItem("Purchased (gal)", quantity(totals.fuel_purchased_gallons));
  textItem("Remaining (gal)", quantity(totals.fuel_remaining_gallons));
  textItem("Trip fuel cost (USD)", String(totals.total_fuel_cost_usd));
  textItem("Routing attempts", String(plan.routing_attempts));

  if (Array.isArray(plan.assumptions) && plan.assumptions.length) {
    assumptionsEl.textContent = plan.assumptions.join(" ");
  }
  if (plan.attribution && plan.attribution.routing) {
    const attribution = document.createElement("p");
    attribution.textContent = plan.attribution.routing;
    summaryEl.appendChild(attribution);
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
      quantity(stop.gallons_purchased) +
      " gal @ $" +
      quantity(stop.price_usd_per_gallon, 4) +
      "/gal = $" +
      Number(stop.cost_usd).toFixed(2);
    root.appendChild(purchase);
    marker.bindPopup(root);
  });

  const bounds = layer.getBounds();
  if (bounds.isValid()) {
    map.fitBounds(bounds, { padding: [24, 24] });
  }
})();
