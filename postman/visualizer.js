// The API already selected stops and calculated costs; this only draws them.
var payload = pm.response.json();
if (pm.response.code === 200 && payload.route && payload.summary) {
    // Keep response numbers intact; round only the labels shown on the map.
    payload.display = {
        distance: payload.summary.total_distance_miles.toFixed(2),
        consumption: payload.summary.total_gallons_consumed.toFixed(2),
        cost: payload.summary.total_fuel_cost.toFixed(2)
    };
    payload.fuel_stops.forEach(function(stop, index) {
        stop.display = {order:index + 1, price:stop.price_per_gallon.toFixed(3),
            purchased:stop.gallons_purchased.toFixed(2), cost:stop.fuel_cost.toFixed(2),
            arrival:stop.arrival_fuel_gallons.toFixed(2), departure:stop.departure_fuel_gallons.toFixed(2),
            detour:stop.detour_miles.toFixed(2)};
    });
    pm.visualizer.set(`
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
 body {font:14px system-ui,sans-serif;color:#203040;margin:16px}
 #map {height:480px;border:1px solid #ccd5df;border-radius:8px}
 .summary {display:flex;gap:24px;flex-wrap:wrap;margin:12px 0}
 .summary strong {display:block;font-size:22px}
 .note {color:#566574;margin:10px 0} #status {color:#8a4300}
 table {border-collapse:collapse;width:100%;margin:12px 0}
 td,th {padding:8px;text-align:left;border-bottom:1px solid #dce2e8}
</style>
<h2>Fuel-optimized trip</h2>
<div class="summary">
 <span>Driven miles<strong>{{display.distance}}</strong></span>
 <span>Gallons consumed<strong>{{display.consumption}}</strong></span>
 <span>Estimated purchases (USD)<strong>&#36;{{display.cost}}</strong></span>
 <span>Fuel stops<strong>{{summary.number_of_stops}}</strong></span>
</div>
<p class="note">The line shows the main route. Station detours are included in the totals,
 but their shapes are unavailable. Intermediate distances and fuel balances are estimates.</p>
<div id="status">Loading map library…</div><div id="map"></div>
<table><thead><tr><th>Stop</th><th>USD/gallon</th><th>Buy gallons</th><th>Cost (USD)</th>
 <th>Fuel on arrival</th><th>Fuel on departure</th><th>Detour miles</th></tr></thead>
<tbody>{{#each fuel_stops}}<tr><td>{{display.order}}. {{name}}</td><td>{{display.price}}</td>
 <td>{{display.purchased}}</td><td>{{display.cost}}</td><td>{{display.arrival}}</td>
 <td>{{display.departure}}</td><td>{{display.detour}}</td></tr>{{/each}}</tbody></table>
<p class="note">{{pricing_assumption}}</p><p class="note">{{route.attribution}}</p>
<p class="note">{{optimization.detour_attribution}}</p>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
var statusBox = document.getElementById('status');
if (typeof L === 'undefined') {
 statusBox.textContent = 'Map library could not load. Check Postman external-resource access. Trip totals and stops remain available below.';
} else {
 pm.getData(function(error, data) {
  if (error) {statusBox.textContent = 'Could not read response data for the map.'; return;}
  try {
   var map = L.map('map');
   var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
   }).addTo(map);
   tiles.on('tileerror', function() {statusBox.textContent = 'Some map tiles could not load. The returned route and markers are still shown.';});
   var line = L.geoJSON(data.route.geometry, {style:{color:'#2563eb', weight:4}}).addTo(map);
   var bounds = line.getBounds();
   function marker(location, text) {
    // Text nodes keep station names and provider labels out of HTML markup.
    var popup = document.createElement('div'); popup.textContent = text;
    L.marker([location.latitude, location.longitude]).addTo(map).bindPopup(popup);
    bounds.extend([location.latitude, location.longitude]);
   }
   marker(data.start, 'Start'); marker(data.destination, 'Destination');
   data.fuel_stops.forEach(function(stop, index) {
    marker(stop, (index + 1) + '. ' + stop.name + ' | $' + stop.price_per_gallon.toFixed(3) +
     '/gal | Buy ' + stop.gallons_purchased.toFixed(2) + ' gal | $' + stop.fuel_cost.toFixed(2));
   });
   map.fitBounds(bounds, {padding:[24,24]});
   statusBox.textContent = data.fuel_stops.length ? 'Click a marker for purchase details.' : 'Starting fuel covers this trip; no purchases are needed.';
   setTimeout(function() {map.invalidateSize();}, 200);
  } catch (failure) {statusBox.textContent = 'Map rendering failed. Trip totals and stop details remain available below.';}
 });
}
</script>`, payload);
}
