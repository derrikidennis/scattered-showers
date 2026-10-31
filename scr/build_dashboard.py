"""
build_dashboard.py : turns the saved CSVs into JSON files and one finished index.html.

Reads   ../assets/readings/*.csv, stations.json, index_template.html
Settings from the environment (the GitHub robot) or a private file (this PC): WU_API_KEY / assets/wu_api_source.md, CARTO_KEY / assets/carto_key.md, NWS_CONTACT / assets/nws_contact.md.
Writes  ../src/dataSource/*.json  and  ../src/index.html
Also asks the National Weather Service for the daily forecast (7 days) and the hourly forecast per station, at most about once an hour.
The Weather Underground API key is read from ../assets/wu_api_source.md and never written into the page.
"""
import base64, csv, json, math, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from wx_lock import file_lock
READ = ROOT / "assets" / "readings"
DS = ROOT / "src" / "dataSource"
GAP_MIN = 10          # a silence longer than this many minutes counts as a gap
RECENT_DAYS = 45      # days kept at full 5-minute detail (older days are hourly in the charts)
FORECAST_MAX_AGE = 3600
# Station colors, in the order of stations.json: Fort Wayne purple, Elgin red, Kingsburg mint.
COLORS = ["#9568f5", "#e85a5a", "#61ffca"]

# Alert colors. The fill is the National Weather Service color blended toward the page's card color so it is not harsh.
# ALERT_STRENGTH is how much of the NWS color is kept (0.80 = 80%). Light colors keep black text; the rest get white text,
# darkened only as far as needed for white text to reach 5.5:1 contrast. Chosen by the user on 2026-10-27.
ALERT_STRENGTH = 0.80
CARD_RGB = (28, 27, 37)

def _lin(v):
    v /= 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

def _lum(rgb):
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])

def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)

def alert_look(hexcolor):
    """Returns (fill, text color) for one alert color."""
    h = hexcolor.lstrip("#")
    c = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    base = [c[i] * ALERT_STRENGTH + CARD_RGB[i] * (1 - ALERT_STRENGTH) for i in range(3)]
    if _lum(base) > 0.35:
        return _hex(base), "#000000"
    f = 1.0
    while (1.05 / (_lum([v * f for v in base]) + 0.05)) < 5.5 and f > 0.2:
        f -= 0.02
    return _hex([v * f for v in base]), "#ffffff"

def load_alerts(path=None):
    """Alerts saved by wx_alerts.py, with the fill and text colors added. Returns {} when there is nothing yet."""
    try:
        doc = json.loads((path or (DS / "alerts.json")).read_text(encoding="utf-8"))
        for st in doc.get("stations", {}).values():
            for a in st.get("alerts", []):
                a["fill"], a["fg"] = alert_look(a.get("hex") or "#808080")
        return doc
    except Exception:
        return {}

def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def avg(v):
    v = [a for a in v if a is not None]
    return round(sum(v) / len(v), 2) if v else None

def hi(v):
    v = [a for a in v if a is not None]
    return max(v) if v else None

def lo(v):
    v = [a for a in v if a is not None]
    return min(v) if v else None

def mean_dir(rows):
    """Average wind direction the right way: turn each reading into an arrow, add the arrows, read the
    direction of the total. Weighted by wind speed, so a calm minute does not outvote a breezy one.
    (350 and 10 degrees average to 0, not 180.) Returns degrees 0-359, or None if there is no data."""
    x = y = wsum = 0.0
    for r in rows:
        if r["wd"] is None:
            continue
        w = r["w"] if r["w"] is not None else 0.0
        a = math.radians(r["wd"])
        x += w * math.sin(a); y += w * math.cos(a); wsum += w
    if wsum == 0:   # dead calm all day: fall back to counting every reading equally
        for r in rows:
            if r["wd"] is None:
                continue
            a = math.radians(r["wd"]); x += math.sin(a); y += math.cos(a)
    if x == 0 and y == 0:
        return None
    return round(math.degrees(math.atan2(x, y)) % 360)

def load(sid):
    rows = []
    for p in sorted(READ.glob(f"{sid}_*.csv")):
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                pmax, pmin = f(r["pressureMax"]), f(r["pressureMin"])
                # Feels like: heat index when 80F or hotter, wind chill when 50F or colder
                # and wind is over 3 mph, otherwise the plain temperature. Uses the
                # heat index and wind chill the station already reports.
                tt, ww = f(r["tempAvg"]), f(r["windspeedAvg"])
                hi, wc = f(r["heatindexAvg"]), f(r["windchillAvg"])
                if tt is not None and tt >= 80 and hi is not None:
                    fl = hi
                elif tt is not None and tt <= 50 and (ww or 0) > 3 and wc is not None:
                    fl = wc
                else:
                    fl = tt
                rows.append({
                    "e": int(r["epoch"]), "loc": r["obsTimeLocal"], "tz": r["tz"],
                    "lat": _r3(f(r["lat"])), "lon": _r3(f(r["lon"])),
                    "t": f(r["tempAvg"]), "th": f(r["tempHigh"]), "tl": f(r["tempLow"]),
                    "fl": fl, "h": f(r["humidityAvg"]),
                    "w": f(r["windspeedAvg"]), "g": f(r["windgustHigh"]), "wd": f(r["winddirAvg"]),
                    "p": avg([pmax, pmin]), "pmax": pmax, "pmin": pmin,
                    "hh": f(r["humidityHigh"]), "hl": f(r["humidityLow"]),
                    "dh": f(r["dewptHigh"]), "dl": f(r["dewptLow"]), "da": f(r["dewptAvg"]),
                    "wh": f(r["windspeedHigh"]), "wl": f(r["windspeedLow"]), "ga": f(r["windgustAvg"]),
                    "rr": f(r["precipRate"]), "rt": f(r["precipTotal"]),
                    "uv": f(r["uvHigh"]), "sol": f(r["solarRadiationHigh"]),
                })
    rows.sort(key=lambda x: x["e"])
    return rows

def load_snow_days(path, sid):
    """Daily snow totals (inches) for one station from the saved hourly history (assets/snow/snow_history.csv).
    Returns {local date: inches}. An hour is labeled with the moment it ENDS, so the hour ending at 00:00 belongs to the day before.
    The first day in the file is left out unless the file starts near its beginning (a half day would read as a quiet day).
    Days before the file starts are simply missing, which the page treats as no snow figure."""
    from datetime import timedelta
    try:
        hrs = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["station"] == sid:
                    hrs.append((datetime.strptime(r["hour_end_local"], "%Y-%m-%d %H:%M"), float(r["snowfall_in"])))
    except Exception:
        return {}
    if not hrs:
        return {}
    hrs.sort()
    out = {}
    for end, v in hrs:
        d = (end - timedelta(seconds=1)).strftime("%Y-%m-%d")
        out[d] = out.get(d, 0.0) + v
    first_end = hrs[0][0]
    first_day = (first_end - timedelta(seconds=1)).strftime("%Y-%m-%d")
    if (first_end - timedelta(seconds=1)).hour > 0:
        out.pop(first_day, None)
    return {k: round(v, 3) for k, v in out.items()}

def station_block(st, rows, snow_days=None):
    if not rows:
        return None
    last = rows[-1]
    today = last["loc"][:10]
    trows = [r for r in rows if r["loc"][:10] == today]
    # daily
    days = {}
    for r in rows:
        days.setdefault(r["loc"][:10], []).append(r)
    daily = []
    for dk in sorted(days):
        v = days[dk]
        eps = [r["e"] for r in v]
        out_min = sum(round((b - a) / 60) for a, b in zip(eps, eps[1:]) if (b - a) > GAP_MIN * 60)
        daily.append({
            "date": dk,
            "hi": max([r["th"] for r in v if r["th"] is not None], default=None),
            "lo": min([r["tl"] for r in v if r["tl"] is not None], default=None),
            "avg": avg([r["t"] for r in v]),
            "hum": avg([r["h"] for r in v]),
            "wind": avg([r["w"] for r in v]),
            "gust": max([r["g"] for r in v if r["g"] is not None], default=None),
            "press": avg([r["p"] for r in v]),
            "rain": max([r["rt"] for r in v if r["rt"] is not None], default=0),
            "snow": (snow_days or {}).get(dk),
            "flHi": hi([r["fl"] for r in v]), "flLo": lo([r["fl"] for r in v]), "flAvg": avg([r["fl"] for r in v]),
            "dpHi": hi([r["dh"] for r in v]), "dpLo": lo([r["dl"] for r in v]), "dpAvg": avg([r["da"] for r in v]),
            "huHi": hi([r["hh"] for r in v]), "huLo": lo([r["hl"] for r in v]), "huAvg": avg([r["h"] for r in v]),
            "wsHi": hi([r["wh"] for r in v]), "wsLo": lo([r["wl"] for r in v]), "wsAvg": avg([r["w"] for r in v]),
            "gHi": hi([r["g"] for r in v]), "gAvg": avg([r["ga"] for r in v]),
            "wdir": mean_dir(v),
            "pHi": hi([r["pmax"] for r in v]), "pLo": lo([r["pmin"] for r in v]),
            "n": len(v), "outMin": out_min, "partial": dk == today,
        })
    # hourly means
    hb = {}
    for r in rows:
        hb.setdefault(r["e"] // 3600 * 3600, []).append(r)
    hourly = []
    for he in sorted(hb):
        v = hb[he]
        hourly.append([he, avg([r["t"] for r in v]), avg([r["fl"] for r in v]), avg([r["h"] for r in v]),
                       avg([r["w"] for r in v]), max([r["g"] for r in v if r["g"] is not None], default=None),
                       avg([r["p"] for r in v])])
    # the last RECENT_DAYS days at full 5-minute detail
    cutoff = last["e"] - RECENT_DAYS * 86400
    recent = [[r["e"], r["t"], r["fl"], r["h"], r["w"], r["g"], r["p"], r["rt"]] for r in rows if r["e"] >= cutoff]
    # gaps
    gaps = []
    for a, b in zip(rows, rows[1:]):
        if (b["e"] - a["e"]) > GAP_MIN * 60:
            gaps.append({"from": a["loc"], "to": b["loc"], "e0": a["e"], "e1": b["e"], "min": round((b["e"] - a["e"]) / 60)})
    # temperature 3 hours ago, for the trend arrow
    ref = last["e"] - 3 * 3600
    past = min(rows, key=lambda r: abs(r["e"] - ref))
    trend = round(last["t"] - past["t"], 1) if (last["t"] is not None and past["t"] is not None and abs(past["e"] - ref) < 1800) else None
    latest = {
        "epoch": last["e"], "local": last["loc"], "tz": last["tz"],
        "temp": last["t"], "feels": last["fl"], "hum": last["h"], "wind": last["w"], "gust": last["g"],
        "dir": last["wd"], "press": last["p"], "rainRate": last["rr"], "rainToday": last["rt"],
        "uv": last["uv"], "trend3h": trend,
        "todayHi": max([r["th"] for r in trows if r["th"] is not None], default=None),
        "todayLo": min([r["tl"] for r in trows if r["tl"] is not None], default=None),
        "todayGust": max([r["g"] for r in trows if r["g"] is not None], default=None),
    }
    return {"id": st["id"], "label": st["label"], "lat": last["lat"], "lon": last["lon"], "tz": last["tz"],
            "first": rows[0]["loc"], "count": len(rows),
            "latest": latest, "daily": daily, "hourly": hourly, "recent": recent,
            "gaps": gaps, "gapMinutes": sum(g["min"] for g in gaps),
            "hasUV": any(r["uv"] not in (None, 0.0) for r in rows),
            "hasSolar": any(r["sol"] not in (None, 0.0) for r in rows)}

def _r3(v):
    """Coordinates are published to 3 decimals only (about a city block)."""
    return None if v is None else round(v, 3)

def _age_s(iso):
    """Seconds since an ISO time stored inside a cache file (None if missing or unreadable).
    Caches go by this stored time, not by the file's modified date, because a fresh copy of the repository gives every file a new date."""
    try:
        return time.time() - datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def get_key():
    k = os.environ.get("WU_API_KEY")
    if k:
        return k.strip()
    m = re.search(r"apiKey=([0-9a-fA-F]+)", (ROOT / "assets" / "wu_api_source.md").read_text(encoding="utf-8"))
    return m.group(1) if m else None

def _wind_num(txt):
    nums = [int(x) for x in re.findall(r"\d+", txt or "")]
    return max(nums) if nums else None

def _c2f(c):
    return None if c is None else round(c * 9 / 5 + 32)

WET_KINDS = ("rain", "storm", "snow", "mix")

def _nws_kind_for_day(icon_url, is_day, pop):
    """Daily icon. A National Weather Service icon address can hold two conditions ('rain_showers,20/ovc').
    With two, a rain chance under 30% shows the dry one and 30% or more shows the wet one."""
    m = re.search(r"/icons/land/(day|night)/([^?]+)", icon_url or "")
    if not m:
        return "cloud"
    codes = [c.split(",")[0] for c in m.group(2).split("/") if c]
    kinds = [_nws_kind(f"/icons/land/{m.group(1)}/{c}", is_day) for c in codes]
    if not kinds:
        return "cloud"
    if len(kinds) == 1:
        return kinds[0]
    wet = [k for k in kinds if k in WET_KINDS]
    dry = [k for k in kinds if k not in WET_KINDS]
    if pop is not None and pop >= 30 and wet:
        return wet[0]
    return dry[0] if dry else kinds[0]

_SKY = r"(sunny|mostly sunny|partly sunny|mostly cloudy|partly cloudy|cloudy|clear|mostly clear|increasing clouds|becoming cloudy|becoming sunny)"
_FILLER = {"a", "an", "the", "of"}

def _norm_words(t):
    return [w for w in re.sub(r"[^a-z ]", " ", (t or "").lower()).split() if w not in _FILLER]

def _extra_text(text, phrase):
    """The National Weather Service paragraph, minus what the card already shows (high, low, wind speed, rain chance,
    plain sky wording, and any sentence that just repeats the short phrase). What is left is the extra: when the rain
    comes, fog, gusts, rainfall amounts. A sentence this does not recognize is kept as it is."""
    pw = _norm_words(phrase)
    segs = [pw]
    if "then" in pw:
        i = pw.index("then")
        segs += [pw[:i], pw[i + 1:]]
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        t = sent.strip()
        if not t:
            continue
        core = t.rstrip(".")
        if re.fullmatch(r"Chance of precipitation is \d+%", core, re.I):
            continue
        if re.fullmatch(r"(?:[A-Za-z]+ ){0,3}wind (?:around |near )?\d+(?: to \d+)? mph", core, re.I):
            continue
        if re.fullmatch(r"(?:Calm|Light and variable) wind", core, re.I):
            continue
        m = re.search(r"gusts as high as (\d+) mph", t, re.I)
        if m and re.match(r"(?:[A-Za-z]+ ){0,3}wind", t, re.I) and not re.match(r"Winds? could", t, re.I):
            out.append(f"Gusts as high as {m.group(1)} mph.")
            continue
        if re.fullmatch(_SKY, core, re.I):
            continue
        if re.fullmatch(r"[A-Za-z ,]+?, with a (?:high|low) (?:near|around) -?\d+", core, re.I):
            continue
        if re.fullmatch(r"(?:High|Low) (?:near|around) -?\d+", core, re.I):
            continue
        m = re.match(r"(?:High|Low) (?:near|around) -?\d+, with (.+)$", core, re.I)
        if m:
            r = m.group(1)
            out.append(r[0].upper() + r[1:] + ".")
            continue
        if pw and _norm_words(core) in segs:
            continue
        out.append(t)
    return " ".join(out)

def forecast_for(blocks):
    """Daily forecast per station from the National Weather Service (12 hour day and night periods, turned into days).
    High = the daytime period (none for tonight). Low = the night period that starts that evening. Rain chance = the higher of the day's periods."""
    path = DS / "forecast.json"
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if all(b["id"] in cached and cached[b["id"]].get("source") == "nws" and cached[b["id"]].get("v") == 4 and cached[b["id"]].get("days")
                   and (_age_s(cached[b["id"]].get("fetched")) or FORECAST_MAX_AGE) < FORECAST_MAX_AGE for b in blocks):
                return cached, "cached"
        except Exception:
            pass
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    out, status = {}, "fresh"
    for b in blocks:
        prev = old.get(b["id"]) or {}
        try:
            urls = prev.get("urls") if prev.get("source") == "nws" else None
            if not urls:
                pt = _nws_get(f"https://api.weather.gov/points/{round(b['lat'], 4)},{round(b['lon'], 4)}")["properties"]
                urls = {"forecast": pt["forecast"], "grid": pt["forecastGridData"]}
            fprops = _nws_get(urls["forecast"])["properties"]
            by = {}
            for p in fprops["periods"]:
                by.setdefault(p["startTime"][:10], []).append(p)
            days = []
            for date in sorted(by)[:7]:
                ps = by[date]
                day_p = next((p for p in ps if p["isDaytime"]), None)
                night_ps = [p for p in ps if not p["isDaytime"]]
                night_p = night_ps[-1] if night_ps else None
                main = day_p or night_p
                tonight = day_p is None          # the daytime is already over: the card is for tonight and has no high
                pops = [(p.get("probabilityOfPrecipitation") or {}).get("value") for p in ps]
                pops = [x for x in pops if x is not None]
                rain = max(pops) if pops else None
                own = (main.get("probabilityOfPrecipitation") or {}).get("value")
                icon = _nws_kind_for_day(main.get("icon"), main.get("isDaytime"), own)
                text = _extra_text(main.get("detailedForecast"), main.get("shortForecast"))
                phrase = main.get("shortForecast")
                # The day looks dry but the night is wet: show the wet icon and say what happens at night.
                if day_p and night_p and rain is not None and rain >= 30 and icon not in WET_KINDS:
                    n_own = (night_p.get("probabilityOfPrecipitation") or {}).get("value")
                    n_kind = _nws_kind_for_day(night_p.get("icon"), False, n_own)
                    if n_kind in WET_KINDS and n_own is not None and n_own >= 30:
                        icon = n_kind
                        # words follow the icon: "Partly Sunny, then Chance Showers And Thunderstorms"
                        phrase = (phrase or "") + ", then " + (night_p.get("shortForecast") or "rain").split(" then ")[-1]
                        # the night's own extra sentences (they carry the timing); the short phrase only if there are none; no sentence twice
                        n_sents = [x for x in re.split(r"(?<=[.!?])\s+", _extra_text(night_p.get("detailedForecast"), night_p.get("shortForecast"))) if x and x not in text]
                        n_text = " ".join(n_sents) if n_sents else (night_p.get("shortForecast") or "Rain") + "."
                        text = (text + " " if text else "") + "That night: " + n_text
                days.append({
                    "day": datetime.strptime(date, "%Y-%m-%d").strftime("%A"), "date": date, "tonight": tonight,
                    "hi": None if tonight else day_p["temperature"], "lo": night_p["temperature"] if night_p else None,
                    "text": text, "rainChance": rain,
                    "phrase": phrase, "wind": _wind_num(main.get("windSpeed")),
                    "windDir": main.get("windDirection") or "",
                    "icon": icon,
                })
            out[b["id"]] = {"days": days, "source": "nws", "v": 4, "urls": urls, "updated": fprops.get("updateTime"),
                            "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            time.sleep(0.5)
        except Exception as e:
            out[b["id"]] = prev if prev.get("source") == "nws" and prev.get("days") else {"days": [], "error": type(e).__name__}
            status = "partly failed"
    path.write_text(json.dumps(out), encoding="utf-8")
    return out, status

def _nws_contact():
    """Contact for the National Weather Service's User-Agent. Not kept in the code (the code is public).
    Order: environment variable NWS_CONTACT (the GitHub robot), then a line 'Contact: ...' in assets/nws_contact.md (this PC), else none."""
    c = os.environ.get("NWS_CONTACT", "").strip()
    if not c:
        try:
            m = re.search(r"^Contact:\s*(\S+)", (ROOT / "assets" / "nws_contact.md").read_text(encoding="utf-8"), re.M)
            c = m.group(1) if m else ""
        except Exception:
            c = ""
    return c

NWS_UA = "ScatteredShowers/1.0" + (" (%s)" % _nws_contact() if _nws_contact() else "")
HOURLY_MAX_AGE = 1800     # the National Weather Service updates its hourly forecast about once an hour
HOURLY_KEEP = 54          # hours kept; the page shows the next 48 from the moment it is opened

def _nws_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def _nws_kind(icon_url, is_day):
    """Turn a National Weather Service icon address into one of the page's own icon names."""
    m = re.search(r"/icons/land/(day|night)/([a-z_]+)", icon_url or "")
    if not m:
        return "cloud"
    c, day = m.group(2), (m.group(1) == "day")
    if c.startswith("wind"):
        return "wind"
    if c.startswith(("tsra", "tornado", "hurricane", "tropical")):
        return "storm"
    if c in ("snow", "blizzard"):
        return "snow"
    if c in ("rain_snow", "rain_sleet", "snow_sleet", "fzra", "rain_fzra", "snow_fzra", "sleet"):
        return "mix"
    if c.startswith("rain"):
        return "rain"
    if c in ("fog", "haze", "smoke", "dust"):
        return "fog"
    if c in ("skc", "hot", "cold"):
        return "sun" if day else "moon"
    if c in ("few", "sct"):
        return "partday" if day else "partnight"
    return "cloud"

def hourly_for(blocks):
    """Next ~2 days, hour by hour, from the National Weather Service (the Weather Underground key is not allowed to ask for hourly)."""
    path = DS / "hourly.json"
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        if all(b["id"] in old and old[b["id"]].get("hours") and (_age_s(old[b["id"]].get("fetched")) or HOURLY_MAX_AGE) < HOURLY_MAX_AGE for b in blocks):
            return old, "cached"
    out, status = {}, "fresh"
    for b in blocks:
        prev = old.get(b["id"]) or {}
        try:
            url = prev.get("url")
            if not url:
                pt = _nws_get(f"https://api.weather.gov/points/{round(b['lat'], 4)},{round(b['lon'], 4)}")
                url = pt["properties"]["forecastHourly"]
            d = _nws_get(url)["properties"]
            hours = []
            for p in d["periods"][:HOURLY_KEEP]:
                wnums = [int(x) for x in re.findall(r"\d+", p.get("windSpeed") or "")]
                hours.append({"t": p["startTime"], "temp": p["temperature"],
                              "pop": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                              "wind": max(wnums) if wnums else None, "dir": p.get("windDirection"),
                              "kind": _nws_kind(p.get("icon"), p.get("isDaytime")), "text": p.get("shortForecast")})
            out[b["id"]] = {"hours": hours, "url": url, "updated": d.get("updateTime"),
                            "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            time.sleep(0.5)
        except Exception as e:
            out[b["id"]] = prev if prev.get("hours") else {"hours": [], "error": type(e).__name__}
            status = "partly failed"
    path.write_text(json.dumps(out), encoding="utf-8")
    return out, status

RECORDS_MAX_AGE = 30 * 86400   # records change a few times a year at most, so the long history is fetched about monthly

def _fetch_records(sid):
    """Record high and low for every calendar day, from the whole daily history of a NOAA climate station (ACIS, free, no key)."""
    body = json.dumps({"sid": sid, "sdate": "por", "edate": "por", "elems": ["maxt", "mint"],
                       "meta": ["name", "valid_daterange"]}).encode("utf-8")
    req = urllib.request.Request("https://data.rcc-acis.org/StnData", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": NWS_UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    days = {}   # "MM-DD" -> [record high, year, record low, year]
    for row in d["data"]:
        try:
            ymd, hi, lo = row[0], row[1], row[2]
        except Exception:
            continue
        key, yr = ymd[5:], int(ymd[:4])
        e = days.setdefault(key, [None, None, None, None])
        if str(hi).lstrip("-").isdigit() and (e[0] is None or int(hi) > e[0]):
            e[0], e[1] = int(hi), yr
        if str(lo).lstrip("-").isdigit() and (e[2] is None or int(lo) < e[2]):
            e[2], e[3] = int(lo), yr
    vr = d["meta"].get("valid_daterange") or [["", ""]]
    return {"name": d["meta"].get("name"), "from": vr[0][0][:4], "to": vr[0][1][:4], "days": days}

def records_for(blocks, cfg):
    path = ROOT / "assets" / "records" / "records.json"
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    want = {st["id"]: st.get("records_sid") for st in cfg["stations"] if st.get("records_sid")}
    mark = path.with_name("records_checked.json")
    try:
        checked = json.loads(mark.read_text(encoding="utf-8")).get("checked")
    except Exception:
        checked = None
    if checked is None and old and all(i in old for i in want):
        checked = datetime.now(timezone.utc).isoformat(timespec="seconds")      # first run with this file: trust the records already saved
        mark.write_text(json.dumps({"checked": checked}), encoding="utf-8")
    fresh_enough = (_age_s(checked) or RECORDS_MAX_AGE) < RECORDS_MAX_AGE
    if fresh_enough and all(i in old for i in want):
        return old, "cached"
    out, status = dict(old), "fresh"
    for i, sid in want.items():
        try:
            out[i] = _fetch_records(sid)
            time.sleep(1)
        except Exception:
            status = "partly failed"     # keep whatever was saved before
    if out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        mark.write_text(json.dumps({"checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
    return out, status

def carto_key():
    """The free CARTO map key: environment variable CARTO_KEY (the GitHub robot), else assets/carto_key.md (a line starting 'Key:'). Empty when neither exists."""
    k = os.environ.get("CARTO_KEY", "").strip()
    if k:
        return k
    try:
        m = re.search(r"^Key:\s*(\S+)", (ROOT / "assets" / "carto_key.md").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""

def read_vendor(name):
    try:
        return (HERE / "vendor" / "leaflet" / name).read_text(encoding="utf-8")
    except Exception:
        return ""

def inject(tpl, data):
    """Fill the template: the data, and the title font (embedded so it looks the same on every device)."""
    font = HERE / "fonts" / "nunito-latin-800-normal.woff2"
    b64 = base64.b64encode(font.read_bytes()).decode("ascii") if font.exists() else ""
    page = tpl.replace("/*__LEAFLET_CSS__*/", read_vendor("leaflet.css")).replace("/*__LEAFLET_JS__*/", read_vendor("leaflet.js"))
    page = page.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    page = page.replace("__CARTO_KEY__", carto_key())
    page = page.replace("__NUNITO_B64__", b64)
    for w in (400, 600, 700):   # body font Inter, embedded; if a file is missing the page falls back to the system font
        f = HERE / "fonts" / f"inter-latin-{w}-normal.woff2"
        page = page.replace(f"__INTER_{w}_B64__", base64.b64encode(f.read_bytes()).decode("ascii") if f.exists() else "")
    return page

def load_snow(path=None):
    """Snowfall saved by wx_snow.py (hourly model estimates). {} when there is nothing yet."""
    try:
        return json.loads((path or (DS / "snow.json")).read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_alert_shapes(path=None):
    """Watch and warning outlines saved by wx_alert_shapes.py. {} when there is nothing yet."""
    try:
        return json.loads((path or (DS / "alert_shapes.json")).read_text(encoding="utf-8"))
    except Exception:
        return {}

def build_all(out_page=None, alerts_path=None, snow_path=None, snow_hist=None, shapes_path=None):
    """out_page: write only the page to this file (tests); the data files and meta.json are left alone."""
    cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
    DS.mkdir(parents=True, exist_ok=True)
    blocks = []
    for st in cfg["stations"]:
        b = station_block(st, load(st["id"]), load_snow_days(snow_hist or (ROOT / "assets" / "snow" / "snow_history.csv"), st["id"]))
        if b:
            blocks.append(b)
    if not blocks:
        print("No readings found. Run wu_collect.py first.")
        return 1
    fc, fstatus = forecast_for(blocks)
    hr, hstatus = hourly_for(blocks)
    rec, rstatus = records_for(blocks, cfg)
    colors = COLORS
    live = {}   # latest live readings written by wu_collect.py; the page uses them only when newer than the history
    try:
        live = json.loads((DS / "live.json").read_text(encoding="utf-8")).get("stations", {})
    except Exception:
        live = {}
    data = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "start": cfg["history_start"],
            "stations": blocks, "forecast": fc, "hourly": hr, "records": rec, "colors": colors, "live": live, "alerts": load_alerts(alerts_path), "alertShapes": load_alert_shapes(shapes_path), "snow": load_snow(snow_path)}
    if not out_page:
        for b in blocks:
            for k in ("latest", "daily", "hourly", "recent", "gaps"):
                (DS / f"{b['id']}_{k}.json").write_text(json.dumps(b[k]), encoding="utf-8")
    tpl = (HERE / "index_template.html").read_text(encoding="utf-8")
    page = inject(tpl, data)
    target = Path(out_page) if out_page else ROOT / "src" / "index.html"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, target)      # the page is whole before anything can see it
    if not out_page:             # meta.json goes last: an open page reloads when it sees a newer build time
        (DS / "meta.json").write_text(json.dumps({"built": data["built"], "stations": [
            {k: b[k] for k in ("id", "label", "lat", "lon", "tz", "first", "count", "gapMinutes")} for b in blocks]}, indent=1), encoding="utf-8")
    print(f"Built {target.name}: {len(blocks)} stations, forecast {fstatus}, hourly {hstatus}, records {rstatus}, {len(page)//1024} KB")
    return 0

def main(argv):
    out_page = alerts_path = snow_path = None
    if "--out" in argv:
        out_page = argv[argv.index("--out") + 1]
    if "--alerts" in argv:
        alerts_path = Path(argv[argv.index("--alerts") + 1])
    snow_path = Path(argv[argv.index("--snow") + 1]) if "--snow" in argv else None
    snow_hist = Path(argv[argv.index("--snow-history") + 1]) if "--snow-history" in argv else None
    shapes_path = Path(argv[argv.index("--shapes") + 1]) if "--shapes" in argv else None
    # Two builds at once (the 15-minute weather task and the 5-minute alerts task fire together) take turns.
    with file_lock(HERE / ".build.lock", wait=150, stale=300) as got:
        if not got:
            print("Another build was still running; skipped this one.")
            return 0
        return build_all(out_page, alerts_path, snow_path, snow_hist, shapes_path)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
