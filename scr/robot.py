"""robot.py : one run of the GitHub robot (started by .github/workflows/robot.yml, or by hand with python scr/robot.py).

What one run does, in order:
  1. Gets the saved history from the `data` branch (a separate branch that holds only data) into _data/ and puts it where the scripts expect it.
  2. Gets readings from Weather Underground. The live reading (the Right now cards) is taken on every run, so every 5 minutes.
     The history (5-minute readings, up to 24 hours back) is fetched only when the last history fetch is 13+ minutes old, so about every 15.
  3. Runs the snow check (it skips itself unless its last check is 55+ minutes old), the alert outlines, and the alert check.
  4. Builds the page once.
  5. Checks the built page and the data files for leaks (scr/publish.py --check). Any problem stops the run: nothing is saved or published.
  6. Puts index.html, dataSource/meta.json and the How To Guide in site/ (the workflow publishes that folder).
  7. Saves the changed data files as one new commit on the `data` branch.

Settings come from the environment (GitHub secrets): WU_API_KEY, NWS_CONTACT, CARTO_KEY, GITHUB_TOKEN. Other settings:
  DRY_RUN=true          do everything except save the data (shows what would change)
  FORCE_COLLECT=true    fetch new readings even if the last fetch was recent
  seed/ folder          if no `data` branch exists yet and the repo has a seed/ folder (publish.py --seed), the branch is created from it
  ALLOW_EMPTY_DATA=true start a brand-new EMPTY `data` branch if none exists and there is no seed (otherwise the run stops, so history is never lost)
  STALE_PAGE_MINUTES=12 the live page counts as stuck when it is this many minutes behind the newest saved data (see page_is_stale)
  BUMP_MAIN_HOURS=24    backstop: an empty commit on main when its newest commit is this old, even if the page looks fine
  ROBOT_REMOTE=...      testing only: use this git address instead of GitHub
  ROBOT_LIVE_URL=...    testing only: read the live page's meta.json from this address instead of the GitHub Pages one
Exit code 0 = fine, 1 = something stopped the run (the workflow then does not publish).
"""
import csv, io, json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCR = BASE / "scr"
DATA = BASE / "_data"
SITE = BASE / "site"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REMOTE = os.environ.get("ROBOT_REMOTE") or f"https://github.com/{REPO}.git"
DRY = os.environ.get("DRY_RUN", "").lower() == "true"
FORCE = os.environ.get("FORCE_COLLECT", "").lower() == "true"
ALLOW_EMPTY = os.environ.get("ALLOW_EMPTY_DATA", "").lower() == "true"
COLLECT_EVERY_S = 13 * 60
BUMP_EVERY_S = float(os.environ.get("BUMP_MAIN_HOURS", "24")) * 3600   # backstop: new empty commit on main when the newest one is this old (see bump_main)
STALE_AFTER_S = float(os.environ.get("STALE_PAGE_MINUTES", "12")) * 60  # live page this far behind the saved data = stuck (see page_is_stale)
BUMP_MIN_GAP_S = 45 * 60                                                # after a stuck-page bump, wait this long before bumping again
IDENT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
STATE_FILES = ["live", "alerts", "alert_shapes", "snow", "forecast", "hourly"]      # src/dataSource/<name>.json
FIXED_FILES = ["assets/snow/snow_history.csv", "assets/alerts/alerts_history.csv", "assets/alerts/zone_cache.json",
               "assets/records/records.json", "assets/records/records_checked.json", "assets/lag/page_lag.csv"]
summary = []
SEEDED = False
LAG = {}                                   # filled by page_is_stale(), written out by record_lag()
LAG_FILE = BASE / "assets" / "lag" / "page_lag.csv"
LAG_COLS = ["checked_utc", "data_commit_utc", "cache_busted_built_utc", "plain_built_utc", "cache_busted_behind_min",
            "plain_behind_min", "plain_cache_age_s", "plain_x_cache", "note",
            "cache_busted_fetched_utc", "plain_fetched_utc", "fetch_gap_s"]   # last three added 2026-11-08: when each fetch finished (older rows leave them empty)
LAG_KEEP = 4320                            # newest rows kept (about 15 days at one run per 5 minutes)


def say(msg):
    print(msg, flush=True)
    summary.append(msg)


def scrub(text):
    return text.replace(TOKEN, "***") if TOKEN else text


def git(args, cwd=None, check=True, push_url=False):
    """Run git. The token never reaches the screen: it is only in the push address and every output is scrubbed."""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    out = scrub((r.stdout or "") + (r.stderr or "")).strip()
    if check and r.returncode != 0:
        raise RuntimeError("git " + scrub(" ".join(a for a in args if not a.startswith("https://x-access-token"))) + " failed: " + out[-400:])
    return r.returncode, out


def push_address():
    if TOKEN and REMOTE.startswith("https://github.com/"):
        return REMOTE.replace("https://", f"https://x-access-token:{TOKEN}@", 1)
    return REMOTE


def data_files(cfg):
    """Every file that belongs on the data branch, as paths relative to BASE (only files that exist)."""
    rels = []
    for st in cfg["stations"]:
        rels += [p.relative_to(BASE).as_posix() for p in sorted((BASE / "assets" / "readings").glob(f"{st['id']}_*.csv"))]
    rels += FIXED_FILES + [f"src/dataSource/{n}.json" for n in STATE_FILES]
    return [r for r in rels if (BASE / r).exists()]


def pull_data():
    """Clone the data branch into _data/ (one commit deep). Returns True when the branch existed."""
    if DATA.exists():
        shutil.rmtree(DATA)
    rc, out = git(["ls-remote", "--heads", REMOTE, "data"], check=False)
    if rc != 0:
        raise RuntimeError("could not reach the repository: " + out[-300:])
    if "refs/heads/data" in out:
        git(["clone", "--quiet", "--depth", "1", "--branch", "data", "--single-branch", REMOTE, str(DATA)])
        return True
    seed = BASE / "seed"
    if seed.is_dir() and any(seed.rglob("*")):
        DATA.mkdir(parents=True)
        git(["init", "--quiet", "-b", "data"], cwd=DATA)
        git(["remote", "add", "origin", REMOTE], cwd=DATA)
        shutil.copytree(seed, DATA, dirs_exist_ok=True)
        say("No data branch yet: it will be created from the seed folder (" + str(sum(1 for p in seed.rglob('*') if p.is_file())) + " files).")
        global SEEDED
        SEEDED = True
        return False
    if not ALLOW_EMPTY:
        raise RuntimeError("there is no `data` branch yet and no seed folder. Put the saved history in the seed folder first "
                           "(python scr\\publish.py --seed on the PC, then commit and push), so the history is never lost. "
                           "To start empty on purpose, run again with ALLOW_EMPTY_DATA=true.")
    DATA.mkdir(parents=True)
    git(["init", "--quiet", "-b", "data"], cwd=DATA)
    git(["remote", "add", "origin", REMOTE], cwd=DATA)
    say("No data branch yet: starting an empty one (ALLOW_EMPTY_DATA).")
    return False


def restore():
    """Put the saved data where the scripts look for it."""
    n = 0
    for sub in ("assets", "src"):
        root = DATA / sub
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    dest = BASE / p.relative_to(DATA)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    n += 1
    return n


def live_age_s():
    try:
        t = json.loads((BASE / "src" / "dataSource" / "live.json").read_text(encoding="utf-8"))["fetched"]
        return time.time() - datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def history_age_s():
    """Seconds since the history was last fetched. live.json remembers it as "historyFetched" (older files only have "fetched")."""
    try:
        d = json.loads((BASE / "src" / "dataSource" / "live.json").read_text(encoding="utf-8"))
        t = d.get("historyFetched") or d["fetched"]
        return time.time() - datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def readings_mode(age, force):
    """"full" = history and live reading, "live" = live reading only."""
    return "full" if (force or age is None or age >= COLLECT_EVERY_S) else "live"


def run(script, args=(), timeout=600):
    r = subprocess.run([sys.executable, str(SCR / script), *args], capture_output=True, text=True, timeout=timeout, cwd=BASE)
    lines = [scrub(l) for l in ((r.stdout or "") + (r.stderr or "")).strip().splitlines() if l.strip()]
    return r.returncode, lines


def sync_out(cfg):
    n = 0
    for rel in data_files(cfg):
        dest = DATA / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BASE / rel, dest)
        n += 1
    return n


def commit_and_push():
    git(["add", "-A"], cwd=DATA)
    rc, _ = git(["diff", "--cached", "--quiet"], cwd=DATA, check=False)
    if rc == 0:
        say("Data: no changes to save.")
        return
    _, stat = git(["diff", "--cached", "--stat"], cwd=DATA)
    if DRY:
        say("Data: DRY RUN, would save:\n" + stat)
        return
    msg = ("Seed data and first run " if SEEDED else "Data ") + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    git([*IDENT, "commit", "--quiet", "-m", msg], cwd=DATA)
    for attempt in (1, 2):
        rc, out = git(["push", "--quiet", push_address(), "HEAD:refs/heads/data"], cwd=DATA, check=False)
        if rc == 0:
            say("Data: saved (" + msg + ").")
            return
        if attempt == 1:                      # someone else saved first: put ours on top of theirs and try once more
            git(["fetch", "--quiet", "--depth", "50", REMOTE, "data"], cwd=DATA, check=False)
            rc2, out2 = git([*IDENT, "rebase", "FETCH_HEAD"], cwd=DATA, check=False)
            if rc2 != 0:
                git(["rebase", "--abort"], cwd=DATA, check=False)
                raise RuntimeError("could not save the data (conflict): " + out2[-300:])
    raise RuntimeError("could not save the data: " + out[-300:])


def page_is_stale():
    """Is the live page stuck? Returns minutes the live page is behind the newest saved data, or None when it cannot be told.
    The newest commit on the data branch is the previous run (it saved right after building that page). The live page's own
    dataSource/meta.json says when its page was built. Healthy: they are about the same. Stuck: the data keeps being saved
    but the live page stays old (the freezes of 2026-11-05 and 2026-11-06, where every step said success)."""
    rc, out = git(["log", "-1", "--format=%ct", "HEAD"], cwd=DATA, check=False) if DATA.is_dir() else (1, "")
    if rc != 0 or not out.strip().isdigit():
        say("Live page check: skipped (no saved data commit to compare with).")
        return None
    data_ts = int(out.strip())
    LAG.clear()
    LAG["data_ts"] = data_ts
    url = os.environ.get("ROBOT_LIVE_URL")
    if not url:
        if "/" not in REPO:
            say("Live page check: skipped (repository name unknown).")
            return None
        owner, name = REPO.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/dataSource/meta.json"
    LAG["plain_url"] = url                                                # the same address without the cache-buster, for record_lag()
    url += ("&" if "?" in url else "?") + f"t={int(time.time())}"        # a new address each time, so no cached copy is returned
    err = ""
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "scattered-showers-robot", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=15) as r:
                built = json.loads(r.read().decode("utf-8"))["built"]
            LAG["busted_fetched_ts"] = time.time()                           # when this fetch finished, for the fetch_gap_s column
            built_ts = datetime.fromisoformat(built.replace("Z", "+00:00")).timestamp()
            behind = (data_ts - built_ts) / 60
            LAG["built_ts"] = built_ts
            say(f"Live page check: live page built {built}, newest saved data {behind:+.0f} min from it (stuck at {STALE_AFTER_S / 60:.0f}+).")
            return behind
        except Exception as e:
            err = scrub(str(e))[:150]
            LAG["err"] = err
            time.sleep(3)
    say("Live page check: skipped (could not read the live page: " + err + ").")
    return None


def record_lag():
    """One line per run in assets/lag/page_lag.csv (kept on the data branch): how far behind the live page is, asked two ways.
    cache_busted_* is the check page_is_stale() just made (a new address, so no cached copy). plain_* asks the normal address the way a
    phone does, with the same request headers, and keeps the cache's Age and X-Cache. If plain lags and cache_busted does not, the cache
    held an old copy. If both lag, GitHub Pages itself was slow to switch. Added 2026-11-07 to find the cause of the 1:46 PM view at 1:55 PM.
    Added 2026-11-08: the time each fetch finished and the gap between the two, so a "the columns disagree" row can be judged
    (a short gap means a deploy landing in between is a live explanation; a long gap does not).
    Never stops a run: the caller catches problems."""
    if not LAG:
        return
    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    def iso_ms(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    data_ts = LAG.get("data_ts")
    row = {"checked_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "data_commit_utc": iso(data_ts) if data_ts else ""}
    note = []
    if LAG.get("built_ts") is not None:
        row["cache_busted_built_utc"] = iso(LAG["built_ts"])
        row["cache_busted_behind_min"] = "%.1f" % ((data_ts - LAG["built_ts"]) / 60)
    elif LAG.get("err"):
        note.append("cache-busted read failed: " + LAG["err"])
    plain_url = LAG.get("plain_url")
    if plain_url:
        try:
            req = urllib.request.Request(plain_url, headers={"User-Agent": "scattered-showers-robot", "Cache-Control": "no-cache", "Pragma": "no-cache"})
            with urllib.request.urlopen(req, timeout=15) as r:
                built = json.loads(r.read().decode("utf-8"))["built"]
                hdr = r.headers
            plain_done = time.time()
            pts = datetime.fromisoformat(built.replace("Z", "+00:00")).timestamp()
            row["plain_built_utc"] = iso(pts)
            row["plain_behind_min"] = "%.1f" % ((data_ts - pts) / 60)
            row["plain_cache_age_s"] = hdr.get("Age") or ""
            row["plain_x_cache"] = hdr.get("X-Cache") or ""
            row["plain_fetched_utc"] = iso_ms(plain_done)
            if LAG.get("busted_fetched_ts") is not None:
                row["cache_busted_fetched_utc"] = iso_ms(LAG["busted_fetched_ts"])
                row["fetch_gap_s"] = "%.2f" % (plain_done - LAG["busted_fetched_ts"])
        except Exception as e:
            note.append("plain read failed: " + scrub(str(e))[:100])
    row["note"] = "; ".join(note)
    head = ",".join(LAG_COLS)
    old = LAG_FILE.read_text(encoding="utf-8").splitlines() if LAG_FILE.exists() else []
    body = [l for l in old if l.strip() and not l.startswith("checked_utc,")]     # drop any header line, old or new (the columns changed on 2026-11-08)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow([row.get(c, "") for c in LAG_COLS])
    body.append(buf.getvalue())
    body = body[-LAG_KEEP:]
    LAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LAG_FILE.with_suffix(".tmp")
    tmp.write_text(head + "\n" + "\n".join(body) + "\n", encoding="utf-8")
    os.replace(tmp, LAG_FILE)


def bump_main(behind_min=None):
    """Add one empty commit to the main branch so the next deploy gets a new commit as its version label.
    Why: GitHub Pages stopped showing new pages (while still saying "success") on 2026-11-05 (after 463 deploys of one commit) and
    again on 2026-11-06 (after about 86). In both cases a new commit on main got the page moving again. Two triggers:
      stuck page: page_is_stale() found the live page STALE_AFTER_S or more behind the data. Bumps if main's newest commit is 45+ min old.
      backstop:   main's newest commit is BUMP_EVERY_S old (24 h), even if the page looks fine (covers a check that cannot read the site).
    Never forces anything: if main moved or the push is refused, it skips and tries again on a later run. A problem here never fails the run."""
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    stuck = behind_min is not None and behind_min * 60 >= STALE_AFTER_S
    rc, out = git(["log", "-1", "--format=%ct", "HEAD"], cwd=BASE, check=False)
    if rc != 0 or not out.strip().isdigit():
        say("Main branch bump: skipped (could not read the newest commit time).")
        return
    age = time.time() - int(out.strip())
    need = BUMP_MIN_GAP_S if stuck else BUMP_EVERY_S
    if age < need:
        if stuck:
            say(f"Main branch bump: page looks stuck ({behind_min:.0f} min behind) but main's newest commit is only {age / 60:.0f} min old; waiting for it to take effect.")
        return
    why = f"live page was {behind_min:.0f} min behind" if stuck else f"newest commit was {age / 3600:.1f} h old"
    if DRY:
        say(f"Main branch bump: DRY RUN, would add an empty commit ({why}).")
        return
    rc, remote = git(["ls-remote", REMOTE, f"refs/heads/{branch}"], check=False)
    rc2, head = git(["rev-parse", "HEAD"], cwd=BASE, check=False)
    if rc != 0 or rc2 != 0 or not remote.split() or remote.split()[0] != head.strip():
        say("Main branch bump: skipped (main has moved or could not be read). Will try again on a later run.")
        return
    title = ("Unstick the page (automatic empty commit, " + why + ")") if stuck else "Keep the page deploys fresh (automatic empty commit)"
    git([*IDENT, "commit", "--quiet", "--allow-empty", "-m", title], cwd=BASE)
    rc, out = git(["push", "--quiet", push_address(), f"HEAD:refs/heads/{branch}"], cwd=BASE, check=False)
    if rc == 0:
        say(f"Main branch bump: added an empty commit ({why}).")
    else:
        say("Main branch bump: push refused, will try again on a later run: " + out[-200:])


def main():
    started = time.time()
    cfg = json.loads((SCR / "stations.json").read_text(encoding="utf-8"))
    existed = pull_data()
    say(f"Data branch: {'found' if existed else 'new'}; restored {restore()} files.")
    try:
        behind = page_is_stale() if existed else None
    except Exception as e:
        behind = None
        say("Live page check: problem, carrying on (" + scrub(str(e))[:200] + ")")
    try:
        if existed:
            record_lag()
    except Exception as e:
        say("Page lag log: problem, carrying on (" + scrub(str(e))[:200] + ")")
    if existed and (BASE / "seed").is_dir():
        say("NOTE: the seed folder is no longer needed. Delete it on the PC with: python scr\\publish.py --remove-seed")

    age = history_age_s()
    if readings_mode(age, FORCE) == "full":
        rc, lines = run("wu_collect.py")
        say("Readings: " + (lines[-1][:300] if lines else "no output") + ("" if rc == 0 else "  [the collector reported a problem; carrying on with the saved data]"))
    else:
        rc, lines = run("wu_collect.py", ["--live-only"])
        say(f"Readings (live only; history was fetched {age / 60:.0f} min ago): " + (lines[-1][:300] if lines else "no output") + ("" if rc == 0 else "  [the collector reported a problem; carrying on with the saved data]"))
    rc, lines = run("wx_snow.py");        say("Snow: " + (lines[-1][:200] if lines else "no output"))
    rc, lines = run("wx_alert_shapes.py"); say("Alert outlines: " + (lines[-1][:200] if lines else "no output"))
    rc, lines = run("wx_alerts.py", ["--no-build"]); say("Alerts: " + (lines[-1][:200] if lines else "no output"))
    rc, lines = run("build_dashboard.py", timeout=300)
    say("Build: " + (lines[-1][:200] if lines else "no output"))
    if rc != 0:
        raise RuntimeError("the page build failed")

    # the page and the guide, ready for the workflow to publish
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "dataSource").mkdir(parents=True)
    shutil.copy2(BASE / "src" / "index.html", SITE / "index.html")
    shutil.copy2(BASE / "src" / "dataSource" / "meta.json", SITE / "dataSource" / "meta.json")
    if (BASE / "docx").exists():
        shutil.copytree(BASE / "docx", SITE / "docx")
    else:
        say("WARNING: no docx/ folder, so the How To Guide is missing from the site.")
    if (BASE / "pwa").exists():          # home-screen icon files and manifest sit at the site root, next to index.html
        for p in sorted((BASE / "pwa").iterdir()):
            if p.is_file():
                shutil.copy2(p, SITE / p.name)
    else:
        say("WARNING: no pwa/ folder, so the home-screen icon is missing from the site.")

    n = sync_out(cfg)
    for folder, kind in ((SITE, "site"), (DATA, "data")):
        rc, lines = run("publish.py", ["--check", str(folder), "--kind", kind])
        say(f"Leak check ({kind}): " + (lines[-1] if lines else "no output") if rc == 0 else f"Leak check ({kind}) FAILED:\n" + "\n".join(lines))
        if rc != 0:
            raise RuntimeError(f"the {kind} leak check found problems; nothing was saved or published")
    commit_and_push()
    try:
        bump_main(behind)
    except Exception as e:
        say("Main branch bump: problem, carrying on (" + scrub(str(e))[:200] + ")")
    say(f"Done in {time.time() - started:.0f} s ({n} data files checked).")


if __name__ == "__main__":
    code = 0
    try:
        main()
    except Exception as e:
        say("STOPPED: " + scrub(str(e)))
        code = 1
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        try:
            with open(sp, "a", encoding="utf-8") as f:
                f.write("### Robot run\n\n" + "\n".join("- " + s.replace("\n", "\n  ") for s in summary) + "\n")
        except OSError:
            pass
    sys.exit(code)
