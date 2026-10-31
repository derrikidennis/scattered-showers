"""
wu_collect.py : pulls 5-minute weather readings from the Weather Underground API
and saves them into one CSV per station per month.

Run it by hand, from Task Scheduler, or from the launcher. No Claude needed.

What one run does, per station:
  1. Asks for the last 24 hours of readings (1 call).
  2. Looks at the newest reading already saved. If there is a gap before the
     last 24 hours (PC was off), it asks for each missing day (1 call per day).
     WU only keeps about 10 days of history, so gaps older than that are lost.
  3. Adds only the rows it doesn't already have (matched on the epoch number),
     so running it twice in a row changes nothing.

After the history work, each run also asks WU for every station's current reading (1 call per
station) and saves them to ../src/dataSource/live.json. That file is only for the dashboard's
Right now cards. It is never added to the CSVs. If the live step fails, history saving is not
affected: it has already finished by then.

Folders (relative to this file):
  ../assets/wu_api_source.md      holds the API key (never printed or logged)
  ../assets/readings/             output CSVs, e.g. KINFORTW445_2026-10.csv
  ../assets/readings/collector.log  one line per run
  stations.json                   station ids, labels, history start date
"""
import csv, json, os, re, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "assets" / "readings"
KEYFILE = ROOT / "assets" / "wu_api_source.md"
LIVE = ROOT / "src" / "dataSource" / "live.json"
BASE = "https://api.weather.com/v2/pws"
PAUSE = 2.1            # seconds between calls, keeps us under 30 calls a minute
DRY = "--dry-run" in sys.argv

TOP = ["stationID", "obsTimeUtc", "obsTimeLocal", "epoch", "tz", "lat", "lon",
       "solarRadiationHigh", "uvHigh", "winddirAvg",
       "humidityHigh", "humidityLow", "humidityAvg", "qcStatus"]
IMP = ["tempHigh", "tempLow", "tempAvg", "windspeedHigh", "windspeedLow", "windspeedAvg",
       "windgustHigh", "windgustLow", "windgustAvg", "dewptHigh", "dewptLow", "dewptAvg",
       "windchillHigh", "windchillLow", "windchillAvg", "heatindexHigh", "heatindexLow",
       "heatindexAvg", "pressureMax", "pressureMin", "pressureTrend", "precipRate", "precipTotal"]
COLS = TOP + IMP

_last_call = [0.0]
class Stop(Exception):
    pass

def get_key():
    k = os.environ.get("WU_API_KEY")
    if k:
        return k.strip()
    try:
        text = KEYFILE.read_text(encoding="utf-8")
    except OSError:
        raise Stop("no Weather Underground key: set the WU_API_KEY setting, or put it in assets/wu_api_source.md")
    m = re.search(r"apiKey=([0-9a-fA-F]+)", text)
    if not m:
        raise Stop("no API key found in " + str(KEYFILE))
    return m.group(1)

def call(path, params, key):
    wait = PAUSE - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    q = dict(params, format="json", units="e", apiKey=key)
    url = f"{BASE}/{path}?{urllib.parse.urlencode(q)}"
    for attempt in range(3):
        try:
            _last_call[0] = time.time()
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read().decode("utf-8")
                if r.status == 204 or not body.strip():
                    return []
                return json.loads(body).get("observations", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                raise Stop(f"HTTP {e.code} from WU (key rejected or rate limit hit), stopping the run")
            if e.code == 204:
                return []
            err = f"HTTP {e.code}"
        except Exception as e:
            err = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(err + " after 3 tries")

def flatten(r):
    imp = r.get("imperial") or {}
    row = {c: r.get(c) for c in TOP}
    for c in ("lat", "lon"):                      # published to 3 decimals only (about a city block); the station's exact spot is never saved
        if row.get(c) is not None:
            try:
                row[c] = "%.3f" % float(row[c])
            except (TypeError, ValueError):
                row[c] = None
    row.update({c: imp.get(c) for c in IMP})
    return row

def _cell(c, v):
    """Every row written gets lat and lon rounded to 3 decimals, including old rows read back from the file."""
    if c in ("lat", "lon") and v not in ("", None):
        try:
            return "%.3f" % float(v)
        except (TypeError, ValueError):
            return ""
    return v

def month_file(sid, local_str):
    return OUT / f"{sid}_{local_str[:7]}.csv"

def load_station(sid):
    """Return {epoch: row} for everything already saved for this station."""
    rows = {}
    for f in sorted(OUT.glob(f"{sid}_*.csv")):
        with open(f, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows[int(row["epoch"])] = row
    return rows

def save_months(sid, new_rows, existing):
    by_month = {}
    for row in new_rows:
        by_month.setdefault(row["obsTimeLocal"][:7], []).append(row)
    for month in by_month:
        merged = {e: r for e, r in existing.items() if r["obsTimeLocal"][:7] == month}
        for r in by_month[month]:
            merged[int(r["epoch"])] = r
        path = OUT / f"{sid}_{month}.csv"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            for e in sorted(merged):
                w.writerow({c: ("" if merged[e].get(c) is None else _cell(c, merged[e].get(c))) for c in COLS})
        os.replace(tmp, path)

def run_station(st, start, key, notes):
    sid = st["id"]
    existing = load_station(sid)
    recent = [flatten(r) for r in call("observations/all/1day", {"stationId": sid}, key)]
    if not recent and not existing:
        notes.append(f"{sid}: no readings returned")
    new = {}
    for r in recent:
        new[int(r["epoch"])] = r
    # figure out which earlier days are missing
    if recent:
        newest_local = max(r["obsTimeLocal"] for r in recent)
        today = datetime.strptime(newest_local[:10], "%Y-%m-%d").date()
    else:
        today = date.today()
    if existing:
        last_saved = max(r["obsTimeLocal"] for r in existing.values())
        first_day = max(start, datetime.strptime(last_saved[:10], "%Y-%m-%d").date())
    else:
        first_day = start
    day, hist_days, hist_empty = first_day, 0, []
    while day < today:
        got = call("history/all", {"stationId": sid, "date": day.strftime("%Y%m%d")}, key)
        hist_days += 1
        if not got:
            hist_empty.append(day.isoformat())
        for r in got:
            f = flatten(r)
            new[int(f["epoch"])] = f
        day += timedelta(days=1)
    # keep only rows we don't have and that are on/after the start date
    fresh = [r for e, r in new.items()
             if e not in existing and r["obsTimeLocal"][:10] >= start.isoformat()]
    if fresh and not DRY:
        save_months(sid, fresh, existing)
    if hist_empty:
        notes.append(f"{sid}: no history for {', '.join(hist_empty)} (outside WU window or station offline)")
    return len(fresh), hist_days

def fetch_live(st, key):
    """One call: the station's current reading, trimmed to what the Right now cards use."""
    obs = call("observations/current", {"stationId": st["id"]}, key)
    if not obs:
        return None
    o = obs[0]
    imp = o.get("imperial") or {}
    if o.get("epoch") is None or imp.get("temp") is None:
        return None
    return {"epoch": int(o["epoch"]), "local": o.get("obsTimeLocal"),
            "temp": imp.get("temp"), "heatIndex": imp.get("heatIndex"), "dewpt": imp.get("dewpt"),
            "windChill": imp.get("windChill"), "windSpeed": imp.get("windSpeed"), "windGust": imp.get("windGust"),
            "pressure": imp.get("pressure"), "precipRate": imp.get("precipRate"), "precipTotal": imp.get("precipTotal"),
            "humidity": o.get("humidity"), "winddir": o.get("winddir"), "uv": o.get("uv"),
            "solar": o.get("solarRadiation")}

def save_live(stations_cfg, key, notes):
    """Take a live reading for each station and merge it into live.json. Never raises."""
    got = 0
    try:
        try:
            old = json.loads(LIVE.read_text(encoding="utf-8")).get("stations", {}) if LIVE.exists() else {}
        except Exception:
            old = {}
        stations = dict(old)
        for st in stations_cfg:
            try:
                r = fetch_live(st, key)
                if r:
                    stations[st["id"]] = r
                    got += 1
                else:
                    notes.append(f"live {st['id']}: no reading returned")
            except Stop as e:
                notes.append("live: " + str(e))
                break
            except Exception as e:
                notes.append(f"live {st['id']}: {e}")
        if got and not DRY:
            LIVE.parent.mkdir(parents=True, exist_ok=True)
            tmp = LIVE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": stations}), encoding="utf-8")
            os.replace(tmp, LIVE)
    except Exception as e:
        notes.append("live: " + type(e).__name__ + ": " + str(e))
    return got

def main():
    cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
    start = datetime.strptime(cfg["history_start"], "%Y-%m-%d").date()
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        key = get_key()
    except Stop as e:
        print(str(e))
        return 1
    notes, parts = [], []
    try:
        for st in cfg["stations"]:
            try:
                n, d = run_station(st, start, key, notes)
                parts.append(f"{st['id']} +{n} rows ({d} history days)")
            except Stop:
                raise
            except Exception as e:
                parts.append(f"{st['id']} FAILED")
                notes.append(f"{st['id']}: {e}")
    except Stop as e:
        notes.append(str(e))
    # skip the live step if WU already refused us (key rejected or rate limit)
    live_got = 0 if any("HTTP" in n for n in notes) else save_live(cfg["stations"], key, notes)
    parts.append(f"live {live_got}/{len(cfg['stations'])}")
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + (" [dry run] " if DRY else " ") + "; ".join(parts)
    if notes:
        line += " | NOTES: " + " / ".join(notes)
    print(line)
    if not DRY:
        lf = OUT / "collector.log"
        old = lf.read_text(encoding="utf-8").splitlines() if lf.exists() else []
        lf.write_text("\n".join((old + [line])[-2000:]) + "\n", encoding="utf-8")
    return 1 if any("FAILED" in p for p in parts) or any("HTTP" in n for n in notes) else 0

if __name__ == "__main__":
    sys.exit(main())
