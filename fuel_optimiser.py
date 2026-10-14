#!/usr/bin/env python3
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dotenv import load_dotenv
load_dotenv()

import requests

API = "https://www.fuel-finder.service.gov.uk/api/v1"
OSRM = "https://router.project-osrm.org"
UA = {"User-Agent": "fuel-optimiser/1.0", "Accept": "application/json"}
LITRES_PER_GALLON = 4.54609
CACHE_FILE = os.path.join(tempfile.gettempdir(), "fuel_optimiser_cache.json")
CACHE_TTL = 30 * 60  # seconds; the API itself is updated within 30 min of any change
FUEL_ALIASES = {"B7_STANDARD": "B7", "B7_PREMIUM": "SDV", "B7P": "SDV", "E5_PREMIUM": "E5"}


# ----------------------------------------------------------------------------- helpers
def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
    return default


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def hav(a, b):
    """Great-circle distance in miles between (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def age_days(ts):
    if not ts:
        return 0.0  # unknown timestamp: keep rather than silently discard
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------------- data
def get_token():
    cid, sec = os.getenv("FUEL_FINDER_CLIENT_ID"), os.getenv("FUEL_FINDER_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("Set FUEL_FINDER_CLIENT_ID and FUEL_FINDER_CLIENT_SECRET (see top of file).")
    r = requests.post(
        f"{API}/oauth/generate_access_token",
        json={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec,
              "scope": "fuelfinder.read"},
        headers={**UA, "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return pick(j, "access_token") or j["data"]["access_token"]


def fetch_all(path, token):
    """Walk the 500-per-batch pagination."""
    out, n = [], 1
    while n <= 60:
        r = requests.get(f"{API}/{path}", params={"batch-number": n}, timeout=90,
                         headers={**UA, "Authorization": f"Bearer {token}"})
        if r.status_code == 404:
            break
        r.raise_for_status()
        j = r.json()
        rows = j.get("data", []) if isinstance(j, dict) else j
        if not rows:
            break
        out += rows
        if len(rows) < 500:
            break
        n += 1
    return out


def build_dataset():
    token = get_token()
    info = {s.get("node_id"): s for s in fetch_all("pfs", token)}
    stations = []
    for p in fetch_all("pfs/fuel-prices", token):
        meta = info.get(p.get("node_id"), {})
        if meta.get("permanent_closure") or meta.get("temporary_closure"):
            continue
        loc = pick(meta, "location") or pick(p, "location") or {}
        lat = num(pick(loc, "latitude", "lat", default=pick(meta, "latitude")))
        lon = num(pick(loc, "longitude", "lon", "lng", default=pick(meta, "longitude")))
        if lat is None or lon is None or not (49 < lat < 61 and -8 < lon < 2.5):
            continue
        fuels = {}
        for fp in p.get("fuel_prices") or []:
            price = num(fp.get("price"))
            if price is None or price <= 0:
                continue
            if price < 10:  # pounds -> pence
                price *= 100
            ft = str(fp.get("fuel_type", "")).upper()
            fuels[FUEL_ALIASES.get(ft, ft)] = {
                "p": price,
                "t": pick(fp, "price_last_updated", "price_change_effective_timestamp")}
        if not fuels:
            continue
        addr = ", ".join(str(x) for x in (pick(loc, "address_line_1"), pick(loc, "city"),
                                          pick(loc, "postcode")) if x)
        stations.append({
            "id": p.get("node_id"),
            "name": pick(p, "trading_name", default=pick(meta, "trading_name", default="Unknown")),
            "brand": pick(meta, "brand_name", "brand", default=""),
            "address": addr, "lat": lat, "lon": lon,
            "fuels": fuels})
    return {"fetched": time.time(), "stations": stations}


_lock = threading.Lock()


def get_dataset():
    with _lock:
        cached = None
        if os.path.exists(CACHE_FILE):
            try:
                cached = json.load(open(CACHE_FILE))
            except (OSError, ValueError):
                cached = None
        if cached and time.time() - cached["fetched"] < CACHE_TTL:
            return cached
        try:
            ds = build_dataset()
            json.dump(ds, open(CACHE_FILE, "w"))
            return ds
        except Exception as e:  # API outages are common: fall back to last good data
            if cached:
                cached["warning"] = f"Live refresh failed ({e}); using cached prices."
                return cached
            raise


# ----------------------------------------------------------------------------- geo / routing
def geocode(text):
    text = text.strip()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", text)
    if m:
        return float(m[1]), float(m[2])
    pc = re.fullmatch(r"[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}", text)
    if pc:
        r = requests.get(f"https://api.postcodes.io/postcodes/{text.replace(' ', '')}", timeout=15)
        if r.ok:
            res = r.json()["result"]
            return res["latitude"], res["longitude"]
    r = requests.get("https://nominatim.openstreetmap.org/search", timeout=15, headers=UA,
                     params={"q": text, "format": "json", "limit": 1, "countrycodes": "gb"})
    j = r.json()
    if not j:
        raise ValueError(f"Could not find location: {text!r}")
    return float(j[0]["lat"]), float(j[0]["lon"])


def osrm_table_miles(origin, dests):
    coords = ";".join(f"{lo},{la}" for la, lo in [origin] + dests)
    r = requests.get(f"{OSRM}/table/v1/driving/{coords}", timeout=30,
                     params={"sources": 0, "annotations": "distance"})
    r.raise_for_status()
    return [None if d is None else d / 1609.344 for d in r.json()["distances"][0][1:]]


def osrm_route(points, full=False):
    coords = ";".join(f"{lo},{la}" for la, lo in points)
    r = requests.get(f"{OSRM}/route/v1/driving/{coords}", timeout=30,
                     params={"overview": "full" if full else "false", "geometries": "geojson"})
    r.raise_for_status()
    rt = r.json()["routes"][0]
    line = [(c[1], c[0]) for c in rt["geometry"]["coordinates"]] if full else None
    return line, rt["distance"] / 1609.344


# ----------------------------------------------------------------------------- optimiser
def eligible(ds, p):
    out = []
    for s in ds["stations"]:
        f = s["fuels"].get(p["fuel"])
        if f and age_days(f["t"]) <= p["max_age"]:
            out.append((s, f))
    return out


def burn_litres(miles, mpg):
    return miles / mpg * LITRES_PER_GALLON


def finalise(rows, p):
    """rows: (station, fuel, detour_miles). Rank by true cost of buying `litres` usable litres."""
    res = []
    for s, f, detour in rows:
        extra = burn_litres(detour, p["mpg"])
        cost = f["p"] / 100 * (p["litres"] + extra)
        res.append({"name": s["name"], "brand": s["brand"], "address": s["address"],
                    "lat": s["lat"], "lon": s["lon"], "price": round(f["p"], 1),
                    "updated": f["t"], "detour_miles": round(detour, 2),
                    "detour_cost": round(f["p"] / 100 * extra, 2),
                    "total_cost": round(cost, 2),
                    "effective_ppl": round(cost * 100 / p["litres"], 2)})
    res.sort(key=lambda r: r["total_cost"])
    if res:
        med = sorted(r["total_cost"] for r in res)[len(res) // 2]
        for i, r in enumerate(res, 1):
            r["rank"] = i
            r["saving_vs_median"] = round(med - r["total_cost"], 2)
    return res[:p["top"]]


def search_nearby(ds, p):
    origin = geocode(p["from"])
    cands = [(s, f, hav(origin, (s["lat"], s["lon"]))) for s, f in eligible(ds, p)]
    cands = [c for c in cands if c[2] <= p["radius"]]
    # Cheap lower bound (straight-line round trip) picks who deserves a real road distance.
    cands.sort(key=lambda c: c[1]["p"] * (p["litres"] + burn_litres(2 * c[2], p["mpg"])))
    cands = cands[:80]
    note = None
    try:
        road = osrm_table_miles(origin, [(c[0]["lat"], c[0]["lon"]) for c in cands])
    except Exception:
        road, note = [None] * len(cands), "Routing server unavailable: used estimated road distances."
    rows = [(s, f, 2 * (r if r is not None else 1.3 * d)) for (s, f, d), r in zip(cands, road)]
    return {"origin": origin, "route": None, "stations": finalise(rows, p), "note": note,
            "considered": len(cands)}


def search_route(ds, p):
    a, b = geocode(p["from"]), geocode(p["to"])
    line, base = osrm_route([a, b], full=True)
    cum = [0.0]
    for i in range(1, len(line)):
        cum.append(cum[-1] + hav(line[i - 1], line[i]))

    cell = p["radius"] / 69.0
    clon = cell / max(0.2, math.cos(math.radians(sum(x[0] for x in line) / len(line))))
    grid = {}
    for i, (la, lo) in enumerate(line):
        grid.setdefault((int(la // cell), int(lo // clon)), []).append(i)

    cands = []
    for s, f in eligible(ds, p):
        gx, gy = int(s["lat"] // cell), int(s["lon"] // clon)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grid.get((gx + dx, gy + dy), ()):
                    d = hav((s["lat"], s["lon"]), line[i])
                    if best is None or d < best[0]:
                        best = (d, i)
        if best and best[0] <= p["radius"]:
            cands.append((s, f, 2 * 1.3 * best[0], cum[best[1]]))  # est. detour = 2 x 1.3 x offset

    cands.sort(key=lambda c: c[1]["p"] * (p["litres"] + burn_litres(c[2], p["mpg"])))
    rows, note = [], None
    for s, f, est, along in cands[:15]:  # refine the most promising with real via-routes
        try:
            _, dist = osrm_route([a, (s["lat"], s["lon"]), b])
            detour = max(0.0, dist - base)
        except Exception:
            detour, note = est, "Some detours are estimates (routing server busy)."
        rows.append((s, f, detour))
    stations = finalise(rows, p)
    step = max(1, len(line) // 1500)
    return {"origin": a, "dest": b, "route": line[::step] + [line[-1]], "route_miles": round(base, 1),
            "stations": stations, "note": note, "considered": len(cands)}


def run_search(body):
    p = {"mode": body.get("mode", "nearby"), "from": body.get("from", ""), "to": body.get("to", ""),
         "mpg": float(body.get("mpg") or 40), "fuel": body.get("fuel", "E10"),
         "litres": float(body.get("litres") or 40), "radius": float(body.get("radius") or 10),
         "max_age": float(body.get("max_age") or 3), "top": 10}
    if p["mpg"] <= 0 or p["litres"] <= 0:
        raise ValueError("MPG and litres must be positive.")
    if not p["from"]:
        raise ValueError("Enter a starting location (postcode, place, or lat,lon).")
    if p["mode"] == "route" and not p["to"]:
        raise ValueError("Enter a destination for route mode.")
    ds = get_dataset()
    out = search_route(ds, p) if p["mode"] == "route" else search_nearby(ds, p)
    out["dataset_age_min"] = round((time.time() - ds["fetched"]) / 60)
    out["stations_loaded"] = len(ds["stations"])
    out["warning"] = ds.get("warning")
    out["params"] = p
    return out


# ----------------------------------------------------------------------------- web UI
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fuel Optimiser</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;display:flex;height:100vh;color:#1a1a1a}
#side{width:400px;overflow:auto;padding:14px;border-right:1px solid #ddd;background:#fafafa}
#map{flex:1}h1{font-size:18px;margin:0 0 10px}label{display:block;font-size:12px;color:#555;margin:8px 0 2px}
input,select{width:100%;padding:7px;border:1px solid #bbb;border-radius:6px;font:inherit}
.row{display:flex;gap:8px}.row>div{flex:1}button{padding:8px 12px;border:0;border-radius:6px;background:#0a7d3c;color:#fff;font:inherit;cursor:pointer}
button.sec{background:#e4e4e4;color:#222;margin-top:4px}#go{width:100%;margin-top:12px;padding:10px;font-weight:600}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:8px;cursor:pointer}
.card:hover{border-color:#0a7d3c}.card.best{border-color:#0a7d3c;border-width:2px}
.top{display:flex;justify-content:space-between;gap:8px;font-weight:600}.sub{color:#555;font-size:12px;margin-top:3px}
.good{color:#0a7d3c}.msg{margin-top:10px;padding:8px;border-radius:6px;background:#fff3cd;font-size:12px}
.pin{background:#2563eb;color:#fff;border-radius:50%;width:26px;height:26px;line-height:26px;text-align:center;font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px #0006}
.pin.best{background:#0a7d3c}
@media(max-width:800px){body{flex-direction:column-reverse}#side{width:100%;height:55%}#map{height:45%}}
</style></head><body>
<div id="side"><h1>⛽ Fuel Optimiser (UK)</h1>
<label>Mode</label><select id="mode"><option value="nearby">Cheapest near me</option><option value="route">Cheapest along a route</option></select>
<label id="fromlbl">Your location (postcode, place or lat,lon)</label>
<input id="from" placeholder="e.g. SO50 6AB"><button class="sec" id="geo">📍 Use my current location</button>
<div id="torow" hidden><label>Destination</label><input id="to" placeholder="e.g. Manchester"></div>
<div class="row"><div><label>Car MPG (UK)</label><input id="mpg" type="number" value="45" min="1" step="0.1"></div>
<div><label>Fuel</label><select id="fuel"><option>E10</option><option>E5</option><option value="B7">Diesel (B7)</option><option value="SDV">Super diesel</option><option>HVO</option></select></div></div>
<div class="row"><div><label>Litres to buy</label><input id="litres" type="number" value="40" min="1"></div>
<div><label id="radlbl">Search radius (miles)</label><input id="radius" type="number" value="10" min="1" step="0.5"></div></div>
<div class="row"><div><label>Ignore prices older than (days)</label><input id="max_age" type="number" value="3" min="0.1" step="0.5"></div></div>
<button id="go">Find best station</button><div id="status"></div><div id="list"></div></div>
<div id="map"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;n'}[c]));
const map=L.map('map').setView([54.0,-3.0],6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap contributors',maxZoom:19}).addTo(map);
const layer=L.layerGroup().addTo(map);let markers=[];
$('mode').onchange=()=>{const r=$('mode').value=='route';$('torow').hidden=!r;
 $('fromlbl').textContent=r?'Start (postcode, place or lat,lon)':'Your location (postcode, place or lat,lon)';$('radlbl').textContent=r?'Max distance from route (miles)':'Search radius (miles)';$('radius').value=r?3:10;};$('geo').onclick=()=>navigator.geolocation.getCurrentPosition(
 p=>$('from').value=p.coords.latitude.toFixed(5)+','+p.coords.longitude.toFixed(5),e=>alert(e.message));
$('go').onclick=async()=>{$('status').innerHTML='<div class="msg">Loading prices & calculating…(first load downloads stations)</div>';$('list').innerHTML='';
 const body={mode:$('mode').value,from:$('from').value,to:$('to').value,mpg:$('mpg').value,fuel:$('fuel').value,
  litres:$('litres').value,radius:$('radius').value,max_age:$('max_age').value};
 try{const r=await fetch('/api/search',{method:'POST',body:JSON.stringify(body)});const d=await r.json();
  if(!r.ok)throw new Error(d.error||r.statusText);render(d);}
 catch(e){$('status').innerHTML='<div class="msg">'+esc(e.message)+'</div>';}};
function render(d){
 layer.clearLayers();markers=[];const pts=[];
 let s=`Prices loaded ${d.dataset_age_min} min ago · ${d.stations_loaded} stations · ${d.considered} candidates checked`;
 if(d.route_miles)s+=` · route ${d.route_miles} mi`;
 [d.warning,d.note].filter(Boolean).forEach(w=>s+='<br>⚠ '+esc(w));
 $('status').innerHTML='<div class="msg">'+s+'</div>';
 if(d.route){L.polyline(d.route,{color:'#2563eb',weight:4,opacity:.7}).addTo(layer);d.route.forEach(p=>pts.push(p));}
 L.circleMarker(d.origin,{radius:7,color:'#000',fillColor:'#fff',fillOpacity:1}).bindTooltip('Start').addTo(layer);pts.push(d.origin);
 if(d.dest){L.circleMarker(d.dest,{radius:7,color:'#000',fillColor:'#000',fillOpacity:1}).bindTooltip('Destination').addTo(layer);}
 if(!d.stations.length){$('list').innerHTML='<div class="msg">No stations found. Try a larger radius or older price limit.</div>';}
 d.stations.forEach((st,i)=>{
  const icon=L.divIcon({className:'',html:`<div class="pin ${i==0?'best':''}">${st.rank}</div>`,iconSize:[26,26]});
  const nav=`https://www.google.com/maps/dir/?api=1&destination=${st.lat},${st.lon}`;
  const m=L.marker([st.lat,st.lon],{icon}).addTo(layer).bindPopup(`<b>${esc(st.name)}</b><br>${st.price}p/L · true cost £${st.total_cost.toFixed(2)}<br><a href="${nav}" target="_blank" rel="noopener">Navigate</a>`);
  markers.push(m);pts.push([st.lat,st.lon]);
  const el=document.createElement('div');el.className='card'+(i==0?' best':'');
  el.innerHTML=`<div class="top"><span>${st.rank}. ${esc(st.name)}</span><span>£${st.total_cost.toFixed(2)}</span></div>
   <div class="sub">${esc(st.brand)} ${esc(st.address)}</div>
   <div class="sub"><b>${st.price}p/L</b> · effective <b>${st.effective_ppl}p/L</b> · detour ${st.detour_miles} mi (£${st.detour_cost.toFixed(2)})</div>
   <div class="sub ${st.saving_vs_median>0?'good':''}">${st.saving_vs_median>=0?'Saves':'Costs'} £${Math.abs(st.saving_vs_median).toFixed(2)} vs median of shortlist · updated ${esc((st.updated||'').slice(0,16).replace('T',' '))}</div>
   <div class="sub"><a href="${nav}" target="_blank" rel="noopener">Navigate ↗</a></div>`;
  el.onclick=e=>{if(e.target.tagName!='A'){map.setView([st.lat,st.lon],14);m.openPopup();}};
  $('list').appendChild(el);});
 if(pts.length)map.fitBounds(pts,{padding:[30,30]});}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/search":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            self._send(200, json.dumps(run_search(body)))
        except (ValueError, KeyError) as e:
            self._send(400, json.dumps({"error": str(e)}))
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))

    def log_message(self, *a):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Fuel Optimiser running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()