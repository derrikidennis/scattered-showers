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
  ROBOT_REMOTE=...      testing only: use this git address instead of GitHub
Exit code 0 = fine, 1 = something stopped the run (the workflow then does not publish).
"""
import csv, json, os, shutil, subprocess, sys, time
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
IDENT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
STATE_FILES = ["live", "alerts", "alert_shapes", "snow", "forecast", "hourly"]      # src/dataSource/<name>.json
FIXED_FILES = ["assets/snow/snow_history.csv", "assets/alerts/alerts_history.csv", "assets/alerts/zone_cache.json",
               "assets/records/records.json", "assets/records/records_checked.json"]
summary = []
SEEDED = False


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


def main():
    started = time.time()
    cfg = json.loads((SCR / "stations.json").read_text(encoding="utf-8"))
    existed = pull_data()
    say(f"Data branch: {'found' if existed else 'new'}; restored {restore()} files.")
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

    n = sync_out(cfg)
    for folder, kind in ((SITE, "site"), (DATA, "data")):
        rc, lines = run("publish.py", ["--check", str(folder), "--kind", kind])
        say(f"Leak check ({kind}): " + (lines[-1] if lines else "no output") if rc == 0 else f"Leak check ({kind}) FAILED:\n" + "\n".join(lines))
        if rc != 0:
            raise RuntimeError(f"the {kind} leak check found problems; nothing was saved or published")
    commit_and_push()
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
