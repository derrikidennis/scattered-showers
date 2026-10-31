"""
wx_alerts.py : asks the National Weather Service (api.weather.gov) which alerts are active at each station's
alert point (the address picked in stations.json as alert_lat / alert_lon), tidies them, and saves:
  ../src/dataSource/alerts.json          what the page shows
  ../assets/alerts/alerts_history.csv    every alert seen, once, for the monthly deep dive
  ../assets/alerts/alerts.log            one line per run
Then it rebuilds the page with build_dashboard.py, unless told not to.

No API key is needed. The NWS asks every caller to send a User-Agent that names the app.
It never raises: if one station's lookup fails, that station keeps its last good alerts, marked as not freshly checked.
Colors, priorities, the hidden types and the watch-hidden-by-warning pairs live in nws_alert_colors.json.

Options
  --no-build       skip the page rebuild (the Refresh button runs its own build)
  --test-dir DIR   write everything to DIR instead of the real places, and skip the build (tests only)
  --fixture FILE   read NWS answers from a JSON file instead of the internet:
                   {"STATION_ID": {"features": [...]}}  or  {"STATION_ID": {"error": "text"}}
  --now EPOCH      pretend this is the current time (tests only)
"""
import csv, json, os, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from wx_lock import file_lock

UA = "ScatteredShowersWeatherDashboard (personal project)"
API = "https://api.weather.gov/alerts/active?point={lat},{lon}"
HIST_COLS = ["key", "station", "place", "event", "severity", "urgency", "certainty",
             "first_seen", "onset", "ends", "expires", "last_seen", "sender", "area", "headline"]

def parse_args(argv):
    o = {"build": True, "test_dir": None, "fixture": None, "now": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-build": o["build"] = False
        elif a == "--test-dir": o["test_dir"] = Path(argv[i + 1]); i += 1
        elif a == "--fixture": o["fixture"] = Path(argv[i + 1]); i += 1
        elif a == "--now": o["now"] = int(argv[i + 1]); i += 1
        i += 1
    if o["test_dir"]: o["build"] = False
    return o

def iso_to_epoch(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def utc_iso(ep):
    return datetime.fromtimestamp(ep, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ep else ""

def fetch(lat, lon):
    """Returns (features, None) or (None, reason). One retry after a short wait."""
    url = API.format(lat=lat, lon=lon)
    last = "no answer"
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8"))
            if not isinstance(j, dict) or not isinstance(j.get("features"), list):
                return None, "unexpected answer"
            return j["features"], None
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
        except Exception as e:
            last = type(e).__name__
        if attempt == 1:
            time.sleep(2)
    return None, last

def tidy(features, C, now):
    """Keep real, current, wanted alerts; add the NWS priority and color; mark watches hidden by a warning."""
    hidden = set(C["hidden"])
    out, seen = [], set()
    for ft in features:
        p = (ft or {}).get("properties") or {}
        ev = p.get("event"); aid = p.get("id") or (ft or {}).get("id")
        if not ev or not aid or aid in seen:
            continue
        if p.get("status") not in (None, "Actual"):      # Test, Exercise, Draft
            continue
        if p.get("messageType") == "Cancel" or ev in hidden:
            continue
        seen.add(aid)
        known = C["aliases"].get(ev, ev)
        t = C["types"].get(known) or C["unknown"]
        start = iso_to_epoch(p.get("onset")) or iso_to_epoch(p.get("effective"))
        ends, expires = iso_to_epoch(p.get("ends")), iso_to_epoch(p.get("expires"))
        end = ends or expires
        if end is not None and end <= now:               # already over
            continue
        refs = p.get("references") or []
        first_ref = refs[0].get("identifier") if refs and isinstance(refs[0], dict) else None
        out.append({"key": first_ref or aid, "id": aid, "event": ev, "pri": t["pri"], "hex": t["hex"],
                    "inGuide": known in C["types"], "severity": p.get("severity"), "urgency": p.get("urgency"),
                    "certainty": p.get("certainty"), "start": start, "end": end, "expires": expires,
                    "sender": p.get("senderName"), "area": p.get("areaDesc"), "headline": p.get("headline"),
                    "description": p.get("description"), "instruction": p.get("instruction")})
    for a in out:                                        # a warning hides its matching watch, only while the warning is active
        warn = C["pairs"].get(a["event"])
        if warn:
            ids = [w["key"] for w in out if w["event"] in warn]
            if ids:
                a["supersededBy"] = ids
    out.sort(key=lambda a: (a["pri"], a["end"] if a["end"] is not None else 1 << 60, a["event"]))
    return out

def update_history(path, sid, place, alerts, now):
    """One row per alert, added the first time it is seen, then last_seen and the end times kept current."""
    rows = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[(r["key"], r["station"])] = r
    for a in alerts:
        k = (a["key"], sid)
        r = rows.get(k) or {"key": a["key"], "station": sid, "place": place, "event": a["event"],
                            "first_seen": utc_iso(now), "onset": utc_iso(a["start"]), "sender": a["sender"] or "",
                            "area": a["area"] or "", "headline": a["headline"] or ""}
        r.update({"severity": a["severity"] or "", "urgency": a["urgency"] or "", "certainty": a["certainty"] or "",
                  "ends": utc_iso(a["end"]) if a["end"] else "", "expires": utc_iso(a["expires"]), "last_seen": utc_iso(now)})
        rows[k] = r
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HIST_COLS)
        w.writeheader()
        for k in sorted(rows, key=lambda k: (rows[k]["first_seen"], k)):
            w.writerow({c: rows[k].get(c, "") for c in HIST_COLS})
    os.replace(tmp, path)

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
        if len(lines) > 3000:
            path.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass

def main(argv):
    o = parse_args(argv)
    now = o["now"] or int(time.time())
    base = o["test_dir"] or None
    out_json = (base / "alerts.json") if base else ROOT / "src" / "dataSource" / "alerts.json"
    hist = (base / "alerts_history.csv") if base else ROOT / "assets" / "alerts" / "alerts_history.csv"
    logf = (base / "alerts.log") if base else ROOT / "assets" / "alerts" / "alerts.log"
    lockf = (base / ".alerts.lock") if base else HERE / ".alerts.lock"
    notes, parts = [], []
    try:
        C = json.loads((HERE / "nws_alert_colors.json").read_text(encoding="utf-8"))
        cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
    except Exception as e:
        log_line(logf, "could not read settings: %s: %s" % (type(e).__name__, e))
        print("Alerts: could not read settings (%s)" % type(e).__name__)
        return 0
    fixture = None
    if o["fixture"]:
        fixture = json.loads(o["fixture"].read_text(encoding="utf-8"))
    with file_lock(lockf, wait=100, stale=300) as got:
        if not got:
            log_line(logf, "skipped: another alerts run was still going")
            print("Alerts: another run is in progress, skipped.")
            return 0
        try:
            old = json.loads(out_json.read_text(encoding="utf-8")).get("stations", {}) if out_json.exists() else {}
        except Exception:
            old = {}
        stations = {}
        for st in cfg["stations"]:
            sid = st["id"]
            lat, lon = st.get("alert_lat"), st.get("alert_lon")
            if lat is None or lon is None:
                notes.append("%s: no alert point in stations.json" % sid)
                continue
            if fixture is not None:
                fx = fixture.get(sid) or {"features": []}
                feats, err = (None, fx["error"]) if "error" in fx else (fx.get("features", []), None)
            else:
                feats, err = fetch(lat, lon)
                time.sleep(0.5)
            if err is None:
                try:
                    alerts = tidy(feats, C, now)
                    stations[sid] = {"ok": True, "checked": now, "point": [lat, lon], "place": st.get("alert_place"),
                                     "alerts": alerts, "error": None}
                    parts.append("%s %d" % (sid, len(alerts)))
                    try:
                        update_history(hist, sid, st.get("alert_place") or "", alerts, now)
                    except Exception as e:
                        notes.append("history %s: %s" % (sid, type(e).__name__))
                    continue
                except Exception as e:
                    err = "could not read the answer (%s)" % type(e).__name__
            prev = old.get(sid) or {}
            stations[sid] = {"ok": False, "checked": prev.get("checked"), "point": [lat, lon], "place": st.get("alert_place"),
                             "alerts": prev.get("alerts", []), "error": err}
            parts.append("%s FAILED (%s)" % (sid, err))
        try:
            write_json(out_json, {"checked": now, "stations": stations})
        except Exception as e:
            notes.append("could not save alerts.json: %s" % type(e).__name__)
    if o["build"]:
        try:
            p = subprocess.run([sys.executable, str(HERE / "build_dashboard.py")], capture_output=True, text=True, timeout=170)
            tail = [l for l in (p.stdout or "").strip().splitlines() if l.strip()]
            parts.append("build " + ("ok" if p.returncode == 0 else "FAILED: " + (tail[-1] if tail else (p.stderr or "")[-120:])))
        except Exception as e:
            notes.append("build: %s" % type(e).__name__)
    line = "Alerts checked: " + "; ".join(parts) + ((" | NOTES: " + "; ".join(notes)) if notes else "")
    log_line(logf, line)
    print(line)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("Alerts: unexpected problem: %s: %s" % (type(e).__name__, e))
        sys.exit(0)
