"""
wx_snow.py : asks Open-Meteo (open-meteo.com, no key) for hourly snowfall at each station's alert point
(alert_lat / alert_lon in stations.json), all three places in ONE request, and saves:
  ../src/dataSource/snow.json            what the page shows (last 72 hours per station)
  ../assets/snow/snow_history.csv        every hour ever fetched, once, for the monthly deep dive
  ../assets/snow/snow.log                one line per run
It does not rebuild the page. The 15-minute weather run (or the Refresh button) does that right after.

Open-Meteo's snowfall is a weather model's estimate, in inches of snow, for the hour that just ended.
Open-Meteo only updates hourly, so a run skips the call when the saved data is younger than --max-age minutes.
It never raises: when the call fails, the last good numbers stay and the file says so.

Options
  --max-age MIN    skip when the saved data is younger than this (default 55)
  --force          fetch no matter how fresh the saved data is
  --test-dir DIR   write everything to DIR instead of the real places (tests only)
  --fixture FILE   read Open-Meteo's answer from a JSON file instead of the internet (tests only)
  --now EPOCH      pretend this is the current time (tests only)
"""
import csv, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from wx_lock import file_lock

UA = "ScatteredShowersWeatherDashboard (personal project)"
API = "https://api.open-meteo.com/v1/forecast"
KEEP_HOURS = 72
HIST_COLS = ["station", "hour_end_epoch", "hour_end_local", "snowfall_in"]


def parse_args(argv):
    o = {"max_age": 55, "force": False, "test_dir": None, "fixture": None, "now": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--force": o["force"] = True
        elif a == "--max-age": o["max_age"] = float(argv[i + 1]); i += 1
        elif a == "--test-dir": o["test_dir"] = Path(argv[i + 1]); i += 1
        elif a == "--fixture": o["fixture"] = Path(argv[i + 1]); i += 1
        elif a == "--now": o["now"] = int(argv[i + 1]); i += 1
        i += 1
    return o


def fetch(points):
    """points: [(lat, lon), ...]. Returns (list of answers in the same order, None) or (None, error text)."""
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p[0]) for p in points), "longitude": ",".join(str(p[1]) for p in points),
        "hourly": "snowfall", "past_days": 3, "forecast_days": 1, "timezone": "auto", "timeformat": "unixtime", "precipitation_unit": "inch"})
    try:
        req = urllib.request.Request(API + "?" + q, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        d = d if isinstance(d, list) else [d]
        if len(d) != len(points):
            return None, "Open-Meteo returned %d places, expected %d" % (len(d), len(points))
        return d, None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])


def hours_from(ans, now):
    """Open-Meteo stamps each value with the END of its hour. The stamp is asked for as an exact moment (unixtime), so the repeated hour when clocks fall back stays two different hours.
    The place's time zone NAME is only used to write the local text.
    Returns ([[epoch_hour_end, inches, local text], ...] up to now, last KEEP_HOURS only)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(ans.get("timezone") or "UTC")
    h = ans["hourly"]
    out = []
    for t, v in zip(h["time"], h["snowfall"]):
        if isinstance(t, int):
            # exact moment from Open-Meteo (timeformat=unixtime). Safe when clocks change: the repeated hour has two different moments.
            e = t
        else:
            # old text form (only used by saved test files). Ambiguous in the repeated hour, so the first one is taken.
            e = int(datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=tz).timestamp())
        if e <= now:
            out.append([e, round(float(v or 0), 3), datetime.fromtimestamp(e, tz).strftime("%Y-%m-%d %H:%M")])
    return out[-KEEP_HOURS:]


def today_total(hours, tzname, now):
    """Snow since local midnight. An hour that ends at 00:00 belongs to the day before."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
        day = datetime.fromtimestamp(now, tz).date()
        return round(sum(h[1] for h in hours if datetime.fromtimestamp(h[0] - 1, tz).date() == day), 3)
    except Exception:
        return None


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def update_history(path, rows):
    """rows: [(station, epoch, local text, inches)]. One line per station and hour; a re-fetch replaces that line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    have = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                have[(r["station"], r["hour_end_epoch"])] = r
    for sid, e, loc, v in rows:
        have[(sid, str(e))] = {"station": sid, "hour_end_epoch": str(e), "hour_end_local": loc, "snowfall_in": "%.3f" % v}
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS)
        w.writeheader()
        for k in sorted(have, key=lambda k: (k[0], int(k[1]))):
            w.writerow(have[k])
    os.replace(tmp, path)


def log_line(path, text):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n")
    except Exception:
        pass


def main(argv):
    o = parse_args(argv)
    now = o["now"] or int(time.time())
    base = o["test_dir"]
    out_json = (base / "snow.json") if base else ROOT / "src" / "dataSource" / "snow.json"
    hist = (base / "snow_history.csv") if base else ROOT / "assets" / "snow" / "snow_history.csv"
    logf = (base / "snow.log") if base else ROOT / "assets" / "snow" / "snow.log"
    lockf = (base / ".snow.lock") if base else HERE / ".snow.lock"
    try:
        cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
        sts = [s for s in cfg["stations"] if s.get("alert_lat") is not None and s.get("alert_lon") is not None]
    except Exception as e:
        log_line(logf, "could not read settings: %s" % type(e).__name__)
        print("Snow: could not read settings (%s)" % type(e).__name__)
        return 0
    with file_lock(lockf, wait=60, stale=300) as got:
        if not got:
            log_line(logf, "skipped: another snow run was still going")
            print("Snow: another run is in progress, skipped.")
            return 0
        try:
            old = json.loads(out_json.read_text(encoding="utf-8")) if out_json.exists() else {}
        except Exception:
            old = {}
        age = (now - old["checked"]) / 60 if old.get("ok") and old.get("checked") else None
        if not o["force"] and age is not None and age < o["max_age"]:
            print("Snow: saved data is %d min old, not asking again yet." % age)
            return 0
        if o["fixture"]:
            ans, err = json.loads(o["fixture"].read_text(encoding="utf-8")), None
        else:
            ans, err = fetch([(s["alert_lat"], s["alert_lon"]) for s in sts])
        if err is None and not (isinstance(ans, list) and len(ans) == len(sts)):
            err = "answer did not match the stations"
        if err is None:
            try:
                stations, hrows = {}, []
                for s, a in zip(sts, ans):
                    hrs = hours_from(a, now)
                    tzname = a.get("timezone") or "UTC"
                    stations[s["id"]] = {"tz": tzname, "today": today_total(hrs, tzname, now),
                                         "hours": [[h[0], h[1]] for h in hrs]}
                    for e, v, loc in hrs:
                        hrows.append((s["id"], e, loc, v))
                write_json(out_json, {"ok": True, "checked": now, "error": None, "stations": stations})
                try:
                    update_history(hist, hrows)
                except Exception as e:
                    log_line(logf, "history not saved: %s" % type(e).__name__)
                msg = "; ".join("%s today %.2f in" % (k, v["today"] or 0) for k, v in stations.items())
                log_line(logf, "ok: " + msg)
                print("Snow checked: " + msg)
                return 0
            except Exception as e:
                err = "could not read the answer (%s)" % type(e).__name__
        # failed: keep the last good numbers, say so
        keep = {"ok": False, "checked": old.get("checked"), "error": err, "stations": old.get("stations", {})}
        try:
            write_json(out_json, keep)
        except Exception:
            pass
        log_line(logf, "FAILED: " + err)
        print("Snow: could not reach Open-Meteo (%s). Keeping the last numbers." % err)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("Snow: unexpected problem (%s: %s)" % (type(e).__name__, e))
        sys.exit(0)
