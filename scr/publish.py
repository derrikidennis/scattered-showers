"""publish.py : the gate between this PC and the public GitHub repository.

Two jobs.
  1. COPY (run on this PC):   python scr\\publish.py
     --seed         also puts the saved history (rounded, leak-checked) in scattered-showers\\seed\\ . The robot builds the `data` branch
                    from it on its first real run, when no data branch exists yet. Delete it afterwards with --remove-seed.
     --remove-seed  deletes scattered-showers\\seed\\ (only after the data branch exists).
     Copies an approved list of files into scattered-showers\\ (the GitHub Desktop folder) and runs the leak checks first and after.
     Nothing outside the approved list is ever copied. Old files from the branch-publishing days (index.html, dataSource\\meta.json)
     are only reported; they are removed only with --remove-old, and only AFTER GitHub Pages has been switched to "GitHub Actions"
     (removing them earlier would blank the live page).
  2. CHECK (run anywhere, the GitHub robot uses it too):   python scr/publish.py --check FOLDER --kind repo|site|data
     Runs the same leak rules on any folder and copies nothing. Exit code 0 = clean, 1 = problems (nothing should be published).

The approved list (copied to the repo's main branch):
  scr/ scripts, page template, stations.json, alert colors, fonts and map library with their licenses, requirements.txt,
  docx/ (the How To Guide and its pictures), .github/workflows/ (the robot's instructions, from scr/workflow/), .gitignore.
NOT in the list, on purpose: assets/ (history, goes to the data branch by the robot), src/ (built fresh by the robot), scr/_backup,
logs, the key files, the .bat and .vbs files that only matter on this PC.

Leak rules (counts and file names are printed, never the private text itself):
  - a forbidden file or folder name (key files, _backup, logs, ...)
  - a latitude or longitude with more than 3 decimals (JSON, page, or a CSV lat/lon column)
  - street-address-looking text
  - an e-mail address
  - a Weather Underground key shape (apiKey=<long hex> or a bare 32-character hex token)
  - the exact secret values, when known: WU key, NWS contact (environment variables on the robot, private files on this PC)
  - the CARTO map key, in kind=repo and kind=data (it is meant to be visible only in the built page)
  - this PC only: the old street addresses and exact coordinates kept in scr/_backup/stations_before_public_*.json
"""
import csv, glob, json, os, re, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../scr
BASE = HERE.parent
SRC = BASE / "src"
PUB = BASE / "scattered-showers"                # the GitHub Desktop repository folder

CODE = ["wu_collect.py", "build_dashboard.py", "wx_alerts.py", "wx_alert_shapes.py", "wx_snow.py", "wx_lock.py", "robot.py", "publish.py"]
OLD_PUBLISHED = ["index.html", "dataSource/meta.json"]     # what branch publishing used; the robot builds these now

GITIGNORE = """# Private or generated. None of this may ever be committed to main. (Written by scr/publish.py.)
/assets/
/src/
/_data/
/site/
scr/_backup/
logs/
followUp/
md/
*.log
*.lock
*.tmp
__pycache__/
wu_api_source.md
carto_key.md
nws_contact.md
"""

TEXT = {".py", ".html", ".json", ".js", ".css", ".yml", ".yaml", ".txt", ".md", ".csv", ".bat", ".vbs", ".gitignore", ".gitattributes"}
BAD_NAMES = {"wu_api_source.md", "carto_key.md", "nws_contact.md", ".env"}
BAD_DIRS = {"_backup", "logs", "followUp", "md", "__pycache__"}

STREET = re.compile(r"\b\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+\s+(?:Ave|Avenue|St|Street|Rd|Road|Dr|Drive|Blvd|Ln|Lane|Ct|Court|Way|Pl|Place)\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
COORD = re.compile(r'"(?:alert_)?(?:lat|lon)"\s*:\s*-?\d+\.\d{4,}')
KEYURL = re.compile(r"apiKey=[0-9a-fA-F]{20,}")
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
# e-mail-looking text that is not an address (none known yet; add exact strings here after checking by hand)
EMAIL_OK = set()


# ---------------------------------------------------------------- secrets and private references
def _file_value(path, pattern):
    try:
        m = re.search(pattern, Path(path).read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None
    except OSError:
        return None

def secret_values(pc):
    """(label, exact text, allowed_in_site) for every secret we can find. Environment first (robot), then private files (this PC)."""
    out = []
    wu = os.environ.get("WU_API_KEY", "").strip() or _file_value(BASE / "assets" / "wu_api_source.md", r"apiKey=([^&\s]+)")
    nws = os.environ.get("NWS_CONTACT", "").strip() or _file_value(BASE / "assets" / "nws_contact.md", r"^Contact:\s*(\S+)")
    carto = os.environ.get("CARTO_KEY", "").strip() or _file_value(BASE / "assets" / "carto_key.md", r"^Key:\s*(\S+)")
    for label, v, site_ok in (("Weather Underground key", wu, False), ("NWS contact", nws, False), ("CARTO map key", carto, True)):
        if v:
            out.append((label, v, site_ok))
    return out

def private_references():
    """This PC only: the old street addresses and exact coordinates. Returns (list, error text or None)."""
    olds = sorted(glob.glob(str(HERE / "_backup" / "stations_before_public_*.json")))
    if not olds:
        return [], "no backup of the old stations.json found, so I cannot tell which private addresses to look for"
    bad = []
    for st in json.load(open(olds[0], encoding="utf-8"))["stations"]:
        place, lat, lon = st.get("alert_place") or "", st.get("alert_lat"), st.get("alert_lon")
        if re.match(r"\s*\d", place):
            bad.append(("old street address", place))
            street = place.split(",")[0].strip()          # the street part alone, in case the town was cut off
            if street and street != place:
                bad.append(("old street address", street))
        for name, v in (("exact latitude", lat), ("exact longitude", lon)):
            if v is not None and len(str(v).split(".")[-1]) > 3:
                bad.append((name, str(v)))
    return bad, None


# ---------------------------------------------------------------- the scanner
def scan(folder, kind, pc=False):
    """Check every file under `folder`. kind: 'repo' (main branch), 'site' (built page), 'data' (history files). Returns a list of problem lines."""
    folder = Path(folder)
    problems = []
    secrets = [(l, v) for l, v, site_ok in secret_values(pc) if not (kind == "site" and site_ok)]
    refs = []
    if pc:
        refs, err = private_references()
        if err:
            return ["STOP: " + err]
    n_files = 0
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        parts = set(rel.split("/")[:-1])
        if ".git" in rel.split("/"):
            continue
        if p.is_dir():
            if p.name in BAD_DIRS or p.name.startswith("_GSA"):
                problems.append(f"{rel}/: a folder that must never be published")
            continue
        n_files += 1
        if p.name in BAD_NAMES or p.suffix == ".log" or p.suffix == ".lock":
            problems.append(f"{rel}: a file that must never be published")
            continue
        if parts & BAD_DIRS:
            continue                                   # already reported at the folder
        if p.suffix.lower() not in TEXT and p.name not in (".gitignore", ".gitattributes"):
            continue                                   # pictures and fonts are not text-checked
        t = p.read_text(encoding="utf-8", errors="ignore")
        found = []
        n = len(COORD.findall(t))
        if n: found.append(f"{n} latitude/longitude value(s) with more than 3 decimals")
        if p.suffix.lower() == ".csv":
            head = t.split("\n", 1)[0].strip().split(",")
            if "lat" in head and "lon" in head:
                n = 0
                for row in csv.DictReader(t.splitlines()):
                    for c in ("lat", "lon"):
                        v = (row.get(c) or "")
                        if "." in v and len(v.split(".")[-1]) > 3:
                            n += 1
                if n: found.append(f"{n} CSV lat/lon value(s) with more than 3 decimals")
        n = len(STREET.findall(t))
        if n: found.append(f"{n} street-address-looking text(s)")
        n = len([e for e in EMAIL.findall(t) if e not in EMAIL_OK])
        if n: found.append(f"{n} e-mail address(es)")
        n = len(KEYURL.findall(t))
        if n: found.append(f"{n} apiKey= with a long hex key")
        n = len(HEX32.findall(t))
        if n: found.append(f"{n} bare 32-character hex token(s) (the shape of a Weather Underground key)")
        for label, v in secrets + refs:
            n = t.count(v)
            if n: found.append(f"{label} appears {n} time(s)")
        for f in found:
            problems.append(f"{rel}: {f}")
    if n_files == 0:
        problems.append(f"{folder}: no files found to check (wrong folder?)")
    return problems


# ---------------------------------------------------------------- the approved list
def approved():
    """[(source path, destination relative to the repo folder, text-checked at the source)]"""
    files = []
    def add(src, dest):
        files.append((src, dest))
    for n in CODE:
        add(HERE / n, f"scr/{n}")
    for n in ("index_template.html", "stations.json", "nws_alert_colors.json"):
        add(HERE / n, f"scr/{n}")
    for p in sorted((HERE / "fonts").glob("*")):
        if p.is_file():
            add(p, f"scr/fonts/{p.name}")
    for p in sorted((HERE / "vendor" / "leaflet").glob("*")):
        if p.is_file():
            add(p, f"scr/vendor/leaflet/{p.name}")
    add(BASE / "requirements.txt", "requirements.txt")
    add(SRC / "docx" / "Scattered_Showers_Guide.html", "docx/Scattered_Showers_Guide.html")
    for p in sorted((SRC / "docx" / "img").glob("*.png")):
        add(p, f"docx/img/{p.name}")
    for p in sorted((HERE / "workflow").glob("*.yml")):          # the robot's instructions (written in step 4b)
        add(p, f".github/workflows/{p.name}")
    return files


def copy_mode(remove_old, seed=False, remove_seed=False):
    files = approved()
    missing = [str(s.relative_to(BASE)) for s, _ in files if not s.exists()]
    if missing:
        print("STOPPED. Approved files missing, nothing was copied:")
        for m in missing: print("  -", m)
        return 1
    # 1. check a staging copy first, so a bad file never reaches the repo folder
    stage = BASE / "_publish_stage"
    if stage.exists():
        shutil.rmtree(stage)
    for s, d in files:
        (stage / d).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, stage / d)
    (stage / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    problems = scan(stage, "repo", pc=True)
    shutil.rmtree(stage, ignore_errors=True)
    # the history files that the robot will keep on the data branch are checked too (not copied here)
    data_problems = scan_data_local()
    problems += ["(data) " + x for x in data_problems]
    if problems:
        print("STOPPED. Nothing was copied to scattered-showers\\. Problems:")
        for x in problems: print("  -", x)
        print("Fix the source and run again.")
        return 1
    # 2. copy
    for s, d in files:
        dest = PUB / d
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dest)
    (PUB / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    # 2b. the seed folder: the saved history, for the robot's first run
    seed_note = None
    if remove_seed:
        shutil.rmtree(PUB / "seed", ignore_errors=True)
        seed_note = "seed\\ folder REMOVED."
    if seed:
        sd = PUB / "seed"
        shutil.rmtree(sd, ignore_errors=True)
        picks = data_picks()
        for src, rel in picks:
            dest = sd / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        post = scan(sd, "data", pc=True)
        if post:
            shutil.rmtree(sd, ignore_errors=True)
            print("STOPPED. The seed folder failed its check and was removed again. Problems:")
            for x in post: print("  -", x)
            return 1
        seed_note = f"seed\\ folder written with {len(picks)} history files (checked). Commit it, run the robot once for real, then run: python scr\\publish.py --remove-seed"
    # 3. report on the repo folder
    keep = {d for _, d in files} | {".gitignore", ".gitattributes"}
    extra = []
    for p in sorted(PUB.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(PUB).parts:
            rel = p.relative_to(PUB).as_posix()
            if rel.startswith("seed/"):
                seed_note = seed_note or "a seed\\ folder is still there. Once the data branch exists, delete it with: python scr\\publish.py --remove-seed"
                continue
            if rel not in keep:
                extra.append(rel)
    old = [r for r in extra if r in OLD_PUBLISHED]
    other = [r for r in extra if r not in OLD_PUBLISHED]
    if remove_old:
        for r in old:
            (PUB / r).unlink()
        for d in ("dataSource",):
            try: (PUB / d).rmdir()
            except OSError: pass
    print(f"Clean. Copied {len(files)} approved files (plus .gitignore) to {PUB}")
    print(f"Checked the staging copy and the history files for: coordinates over 3 decimals, street addresses, e-mail addresses, key shapes, "
          f"{len(secret_values(True))} known secret value(s), and the old private addresses.")
    if seed_note:
        print(seed_note)
    if old:
        print(("REMOVED" if remove_old else "STILL THERE (leave them until Pages is switched to GitHub Actions; then run with --remove-old)") + ": " + ", ".join(old))
    if other:
        print("NOT on the approved list, left alone (look at these before you commit): " + ", ".join(other))
    return 0


def data_picks():
    """[(source path, path relative to BASE)] for the files the robot keeps on the data branch (only those that exist)."""
    cfg = json.loads((HERE / "stations.json").read_text(encoding="utf-8"))
    picks = []
    for st in cfg["stations"]:
        picks += sorted((BASE / "assets" / "readings").glob(f"{st['id']}_*.csv"))
    for rel in ("snow/snow_history.csv", "alerts/alerts_history.csv", "alerts/zone_cache.json", "records/records.json", "records/records_checked.json"):
        picks.append(BASE / "assets" / rel)
    for rel in ("live", "alerts", "alert_shapes", "snow", "forecast", "hourly"):
        picks.append(SRC / "dataSource" / f"{rel}.json")
    return [(p, p.relative_to(BASE).as_posix()) for p in picks if p.exists()]


def scan_data_local():
    """The files the robot keeps on the data branch, checked here before the first upload."""
    try:
        picks = data_picks()
    except Exception as e:
        return [f"cannot read stations.json: {type(e).__name__}"]
    stage = BASE / "_publish_stage_data"
    if stage.exists():
        shutil.rmtree(stage)
    for src, rel in picks:
        dest = stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    probs = scan(stage, "data", pc=True)
    shutil.rmtree(stage, ignore_errors=True)
    return probs


def check_mode(folder, kind):
    problems = scan(folder, kind, pc=False)
    if problems:
        print(f"STOPPED. {kind} check found problems in {folder}:")
        for x in problems: print("  -", x)
        return 1
    print(f"Clean. {kind} check passed for {folder} ({len(secret_values(False))} known secret value(s) searched for).")
    return 0


def main(argv):
    if "--check" in argv:
        i = argv.index("--check")
        kind = argv[argv.index("--kind") + 1] if "--kind" in argv else "repo"
        if kind not in ("repo", "site", "data"):
            print("--kind must be repo, site or data"); return 2
        return check_mode(argv[i + 1], kind)
    return copy_mode("--remove-old" in argv, "--seed" in argv, "--remove-seed" in argv)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
