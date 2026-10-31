"""
wx_alert_shapes.py : gets the OUTLINES of active National Weather Service watches and warnings near each station, so the
radar map can draw them. Saves:
  ../src/dataSource/alert_shapes.json     what the page draws
  ../assets/alerts/zone_cache.json        outlines of NWS zones (counties), looked up once and remembered
  ../assets/alerts/alert_shapes.log       one line per run
Run by alerts.bat just before wx_alerts.py (which rebuilds the page). It never rebuilds the page itself.

How it works: for each state that has a station, ask api.weather.gov/alerts/active?area=ST. Warnings usually come with a
storm-shaped outline. Watches (and some warnings) come as a list of zones instead, so each zone's outline is looked up
once and cached. Only watches and warnings are kept (no advisories or statements). An alert is kept when its outline
comes within RADIUS_MI of a station. If a state's lookup fails, that state keeps its last good outlines.

Options
  --test-dir DIR   write everything to DIR instead of the real places
  --fixture FILE   read NWS answers from a file: {"alerts": {"TX": [features]}, "zones": {"zone url": geometry}}
  --now EPOCH      pretend this is the current time (tests only)
"""
import json, math, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from wx_lock import file_lock

UA = "ScatteredShowersWeatherDashboard (personal project)"
AREA = "https://api.weather.gov/alerts/active?area={st}"
RADIUS_MI = 300          # outlines within this many miles of a station are kept
ZONE_BUDGET = 60         # new zone lookups per run; the rest wait for the next run
SIMPLIFY = 0.004         # degrees (about 400 m); keeps files small
ZONE_KEEP_DAYS = 120

def parse_args(argv):
    o = {"test_dir": None, "fixture": None, "now": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--test-dir": o["test_dir"] = Path(argv[i + 1]); i += 1
        elif a == "--fixture": o["fixture"] = Path(argv[i + 1]); i += 1
        elif a == "--now": o["now"] = int(argv[i + 1]); i += 1
        i += 1
    return o

def iso_to_epoch(s):
    if not s: return None
    try: return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception: return None

def http_json(url):
    last = "no answer"
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 404): break
        except Exception as e:
            last = type(e).__name__
        if attempt == 1: time.sleep(2)
    return None, last

# ---------- geometry (points are [lat, lon]) ----------
def dp(pts, tol):
    """Douglas-Peucker, no recursion. Keeps first and last point."""
    n = len(pts)
    if n < 3: return pts
    keep = [False] * n; keep[0] = keep[-1] = True; stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop(); best, idx = 0.0, -1
        (ay, ax), (by, bx) = pts[a], pts[b]; dy, dx = by - ay, bx - ax; L = dy * dy + dx * dx
        for k in range(a + 1, b):
            py, px = pts[k]
            if L == 0: d = math.hypot(py - ay, px - ax)
            else:
                t = max(0, min(1, ((py - ay) * dy + (px - ax) * dx) / L)); d = math.hypot(py - (ay + t * dy), px - (ax + t * dx))
            if d > best: best, idx = d, k
        if best > tol and idx > 0:
            keep[idx] = True; stack.append((a, idx)); stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]

def to_polys(g):
    """GeoJSON Polygon or MultiPolygon -> [[ring, ring...], ...] with [lat, lon] points, rounded and simplified."""
    if not isinstance(g, dict): return []
    t, c = g.get("type"), g.get("coordinates")
    polys = [c] if t == "Polygon" else (c if t == "MultiPolygon" else [])
    out = []
    for poly in polys or []:
        rings = []
        for ring in poly:
            pts = [[round(p[1], 3), round(p[0], 3)] for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
            pts = dp(pts, SIMPLIFY)
            if len(pts) >= 4: rings.append(pts)
        if rings: out.append(rings)
    return out

def merge(polys):
    """Join county outlines into one shape per watch (no lines between neighbours). Needs the shapely package;
    without it, or on any trouble, the separate county outlines are returned unchanged."""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        gap = 0.004                                  # closes the hairline gaps left by rounding and simplifying
        shapes = [Polygon([(p[1], p[0]) for p in poly[0]], [[(p[1], p[0]) for p in r] for r in poly[1:]]).buffer(gap, join_style=2) for poly in polys]
        u = unary_union(shapes).buffer(-gap, join_style=2).simplify(SIMPLIFY / 2)
        parts = list(u.geoms) if hasattr(u, "geoms") else [u]
        out = []
        for g in parts:
            if g.is_empty or g.geom_type != "Polygon": continue
            rings = [[[round(y, 3), round(x, 3)] for x, y in g.exterior.coords]] + [[[round(y, 3), round(x, 3)] for x, y in i.coords] for i in g.interiors]
            out.append([r for r in rings if len(r) >= 4])
        return out or polys
    except Exception:
        return polys

def bbox(polys):
    ys = [p[0] for poly in polys for r in poly for p in r]; xs = [p[1] for poly in polys for r in poly for p in r]
    return (min(ys), min(xs), max(ys), max(xs)) if ys else None

def near(polys, lat, lon, miles=RADIUS_MI):
    b = bbox(polys)
    if not b: return False
    dlat = miles / 69.0; dlon = miles / (69.0 * max(0.2, math.cos(math.radians(lat))))
    return b[0] - dlat <= lat <= b[2] + dlat and b[1] - dlon <= lon <= b[3] + dlon

def contains(polys, lat, lon):
    for poly in polys:
        r = poly[0]; ins = False; j = len(r) - 1
        for i in range(len(r)):
            yi, xi, yj, xj = r[i][0], r[i][1], r[j][0], r[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi: ins = not ins
            j = i
        if ins: return True
    return False

def state_of(st):
    if st.get("alert_state"): return st["alert_state"].upper()
    m = re.search(r",\s*([A-Z]{2})\s*$", st.get("alert_place") or "")
    return m.group(1) if m else None

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)

def log_line(path, text):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > 3000: path.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass

def wanted(p, C, now):
    ev = p.get("event")
    if not ev or not (ev.endswith(" Warning") or ev.endswith(" Watch")): return None
    if ev in set(C["hidden"]) or p.get("status") not in (None, "Actual") or p.get("messageType") == "Cancel": return None
    end = iso_to_epoch(p.get("ends")) or iso_to_epoch(p.get("expires"))
    if end is not None and end <= now: return None
    known = C["aliases"].get(ev, ev); t = C["types"].get(known) or C["unknown"]
    return {"event": ev, "pri": t["pri"], "hex": t["hex"], "end": end,
            "kind": "watch" if ev.endswith(" Watch") else "warning"}

def main(argv):
    o = parse_args(argv); now = o["now"] or int(time.time()); base = o["test_dir"]
    out_json = (base / "alert_shapes.json") if base else ROOT / "src" / "dataSource" / "alert_shapes.json"
    zfile = (base / "zone_cache.json") if base else ROOT / "assets" / "alerts" / "zone_cache.json"
    logf = (base / "alert_shapes.log") if base else ROOT / "assets" / "alerts" / "alert_shapes.log"
    lockf = (base / ".shapes.lock") if base else HERE / ".shapes.lock"
    try:
        C = json.loads((HERE / "nws_alert_colors.json").read_text(encoding="utf-8"))
        cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
    except Exception as e:
        log_line(logf, "could not read settings: %s" % type(e).__name__); print("Shapes: could not read settings"); return 0
    fixture = json.loads(o["fixture"].read_text(encoding="utf-8")) if o["fixture"] else None
    with file_lock(lockf, wait=100, stale=300) as got:
        if not got:
            log_line(logf, "skipped: another run was still going"); print("Shapes: another run is in progress, skipped."); return 0
        try: old = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else {}
        except Exception: old = {}
        try: zones = json.loads(zfile.read_text(encoding="utf-8")) if zfile.exists() else {}
        except Exception: zones = {}
        stations_cfg = [(st["id"], st.get("alert_lat"), st.get("alert_lon"), state_of(st)) for st in cfg["stations"]]
        states = sorted({s for _, la, lo, s in stations_cfg if s and la is not None})
        shapes, per_station, notes, budget, new_zones, partial = {}, {}, [], ZONE_BUDGET, 0, 0
        failed = set()
        for stt in states:
            if fixture is not None: feats, err = (fixture.get("alerts", {}).get(stt, []), None)
            else:
                j, err = http_json(AREA.format(st=stt)); feats = (j or {}).get("features") if j else None
                if err is None and not isinstance(feats, list): err = "unexpected answer"
                time.sleep(0.5)
            if err:
                failed.add(stt); notes.append("%s lookup failed (%s), kept last outlines" % (stt, err)); continue
            cand = []
            for ft in feats:
                p = (ft or {}).get("properties") or {}; w = wanted(p, C, now); aid = p.get("id") or (ft or {}).get("id")
                if w and aid: cand.append((w["pri"], aid, ft, p, w))
            cand.sort(key=lambda c: (c[0], str(c[1])))
            for _, aid, ft, p, w in cand:
                if aid in shapes: continue
                polys = to_polys((ft or {}).get("geometry")); incomplete = False
                if not polys:
                    for zu in p.get("affectedZones") or []:
                        z = zones.get(zu)
                        if z is None:
                            if budget <= 0: incomplete = True; continue
                            if fixture is not None: g = fixture.get("zones", {}).get(zu); zerr = None if g else "not in fixture"
                            else: zj, zerr = http_json(zu); g = (zj or {}).get("geometry"); time.sleep(0.3)
                            budget -= 1
                            if zerr or not g: incomplete = True; continue
                            z = {"polys": to_polys(g), "saved": now}; zones[zu] = z; new_zones += 1
                        polys.extend(z["polys"])
                if not polys: partial += 1; continue
                if len(polys) > 1 and not (ft or {}).get("geometry"): polys = merge(polys)
                if incomplete: partial += 1
                shapes[aid] = {"id": aid, "event": w["event"], "kind": w["kind"], "pri": w["pri"], "hex": w["hex"], "end": w["end"],
                               "headline": p.get("headline"), "area": (p.get("areaDesc") or "")[:200], "sender": p.get("senderName"),
                               "partial": incomplete, "polys": polys}
        for sid, la, lo, stt in stations_cfg:
            if la is None or lo is None: continue
            if stt in failed:
                per_station[sid] = (old.get("stations") or {}).get(sid, []); continue
            ids = []
            for aid, s in shapes.items():
                if near(s["polys"], la, lo): ids.append(aid)
            ids.sort(key=lambda a: (shapes[a]["pri"], a)); per_station[sid] = ids
        for sid in per_station:                              # reuse last outlines for a failed state
            for aid in per_station[sid]:
                if aid not in shapes:
                    s = (old.get("shapes") or {}).get(aid)
                    if s and (s.get("end") is None or s["end"] > now): shapes[aid] = s
        used = {a for ids in per_station.values() for a in ids}
        for sid in per_station:
            per_station[sid] = [a for a in per_station[sid] if a in shapes]
        shapes = {a: s for a, s in shapes.items() if a in used}
        for sid, la, lo, stt in stations_cfg:                # is the address inside the outline
            for aid in per_station.get(sid, []):
                s = shapes[aid]; s.setdefault("hit", {})[sid] = contains(s["polys"], la, lo)
        cutoff = now - ZONE_KEEP_DAYS * 86400
        zones = {u: z for u, z in zones.items() if z.get("saved", now) >= cutoff}
        try:
            write_json(out_json, {"checked": now, "ok": not failed, "errors": notes, "shapes": shapes, "stations": per_station})
            if new_zones: write_json(zfile, zones)
        except Exception as e:
            notes.append("could not save: %s" % type(e).__name__)
    line = "Shapes: %d outlines kept (%s); %d new zones, %d without a complete outline" % (
        len(shapes), ", ".join("%s %d" % (k, len(v)) for k, v in per_station.items()), new_zones, partial)
    if notes: line += " | NOTES: " + "; ".join(notes)
    log_line(logf, line); print(line); return 0

if __name__ == "__main__":
    try: sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("Shapes: unexpected problem: %s: %s" % (type(e).__name__, e)); sys.exit(0)
