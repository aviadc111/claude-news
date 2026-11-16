"""
Keeps the stock check on time.

GitHub is asked to start the job every 15 minutes, but in practice it starts it only every
1 to 2.5 hours. So a run that starts inside "busy hours" stays alive and repeats the check
itself on every quarter hour:
  - Monday to Friday, 06:00 to 16:20 New York time. The US market is open 09:30 to 16:00;
    the early start makes sure a run is already alive when it opens.
  - the weekly-message day, from 3.5 hours before the message time until 10 minutes after
    it, so the Sunday 08:30 message leaves at 08:30.
Outside busy hours a run checks once and ends, as before.

Between two checks the run does not sleep: it listens on the app's settings channel. When a
stock is added, removed or picked in the app, the check runs a few seconds later instead of
at the next quarter hour. After a check that applied such a change, a short note goes to the
app's status channel, so an open app fetches the new data at once.

Runs hand over to each other. Only one run may work at a time, so a newer run that GitHub
starts has to wait. When this run sees one waiting, it ends after its current check and the
newer one carries on. A run also ends by itself after MAX_HOURS, which is under GitHub's
6-hour limit for a job.

Every check: fetch the newest data files, run worker/run.py, commit data/ if it changed.

    python worker/loop.py            used by the workflow
    python worker/loop.py --once     one check, then end
    python worker/loop.py --dry      checks send nothing and nothing is committed
    python worker/loop.py --plan     print when the busy hours are, run nothing
"""

import datetime as dt
import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG_PATH = os.path.join(HERE, "config.json")

STEP_MINUTES = 15                 # a check on every quarter hour
TICK_OFFSET_SECONDS = 15          # a little after the quarter, never a second before it
MAX_HOURS = 5.3                   # GitHub stops a job at 6 hours
MARKET_TZ = "America/New_York"
MARKET_FROM = dt.time(6, 0)       # 3.5 hours before the open: GitHub can take 2.5 hours to start a run
MARKET_UNTIL = dt.time(16, 20)    # the close is 16:00; the 16:15 and 16:30 checks pick up the final price
WEEKLY_LEAD = dt.timedelta(hours=3, minutes=30)
WEEKLY_TAIL = dt.timedelta(minutes=10)
SETTLE_SECONDS = 4                # the app saves a moment after the last tap: wait for quick changes to finish
LISTEN_SECONDS = 40               # one listening connection; a new one follows straight away
MAX_EARLY_CHECKS = 80             # per run, so a flood of changes cannot keep the job checking nonstop


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def busy(now, cfg):
    """Why this moment is inside busy hours ("market" / "weekly"), or "" when it is not."""
    ny = now.astimezone(ZoneInfo(MARKET_TZ))
    if ny.weekday() < 5 and MARKET_FROM <= ny.time() < MARKET_UNTIL:
        return "market"
    w = cfg.get("weekly") or {}
    if w.get("time"):
        local = now.astimezone(ZoneInfo(w.get("timezone", "Asia/Jerusalem")))
        if local.weekday() == int(w.get("weekday", 6)):
            hh, mm = (int(x) for x in w["time"].split(":"))
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target - WEEKLY_LEAD <= local <= target + WEEKLY_TAIL:
                return "weekly"
    return ""


def next_tick(now):
    """The next quarter hour, plus a few seconds."""
    base = now.replace(second=0, microsecond=0)
    base -= dt.timedelta(minutes=base.minute % STEP_MINUTES)
    tick = base + dt.timedelta(seconds=TICK_OFFSET_SECONDS)
    while tick <= now:
        tick += dt.timedelta(minutes=STEP_MINUTES)
    return tick


def newer_run_waiting(env=None):
    """True when GitHub has started a newer run of this workflow that has not finished."""
    env = os.environ if env is None else env
    repo, mine = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_NUMBER")
    if not repo or not mine:
        return False
    ref = env.get("GITHUB_WORKFLOW_REF", "")              # owner/repo/.github/workflows/stocks.yml@refs/...
    workflow = ref.split("@")[0].rsplit("/", 1)[-1] if "/workflows/" in ref else "stocks.yml"
    headers = {"Accept": "application/vnd.github+json"}
    if env.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + env["GITHUB_TOKEN"]
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs",
                         params={"per_page": 10}, headers=headers, timeout=20)
        r.raise_for_status()
        return any(run["run_number"] > int(mine) and run["status"] != "completed"
                   for run in r.json().get("workflow_runs", []))
    except Exception as e:                                   # not being able to ask is not a reason to stop
        print("could not ask GitHub about newer runs:", type(e).__name__, flush=True)
        return False


def app_channel(name):
    """Address of one of the app's ntfy channels ("settings", "status"), or None."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import claude_news_core as cn
        c = cn.load_app_config()
        return f"{c['ntfy'].rstrip('/')}/{c['topic']}-{name}"
    except Exception as e:
        print("no app channel:", type(e).__name__, flush=True)
        return None


def settings_mark():
    """Id of the newest app change the checker has applied (kept in data/settings.json)."""
    try:
        with open(os.path.join(ROOT, "data", "settings.json"), encoding="utf-8") as f:
            return json.load(f).get("since")
    except (OSError, ValueError):
        return None


def wait_for_change(url, since, until, now=utc_now, get=None):
    """Wait until `until`. Returns the id of a change made in the app in the meantime, else None.

    `since` is the id of the last change already seen (or a unix time): anything newer counts,
    also a change that arrived while a check was running.
    """
    get = get or requests.get
    complained = False
    while True:
        left = (until - now()).total_seconds()
        if left <= 0:
            return None
        if not url:
            time.sleep(min(left, 30))
            continue
        span = min(left, LISTEN_SECONDS)
        began = time.monotonic()
        try:
            with get(url + "/json", params={"since": since}, stream=True, timeout=(min(10, max(span, 1)), span)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        try:
                            m = json.loads(line)
                        except ValueError:
                            continue
                        if m.get("event") == "message":
                            return m.get("id") or "change"
                    if time.monotonic() - began >= span:
                        break
        except Exception as e:
            if time.monotonic() - began < max(span - 1, 0.5):   # not just the quiet end of a connection
                if not complained:
                    print("could not listen for app changes, trying again:", type(e).__name__, flush=True)
                    complained = True
                time.sleep(max(0, min(15, (until - now()).total_seconds())))
        if time.monotonic() - began < 0.5:                   # a connection that ended at once: never spin
            time.sleep(max(0, min(1, (until - now()).total_seconds())))


def tell_app(dry=False):
    """A short note on the status channel: new data is in the repository. An open app fetches it."""
    url = app_channel("status")
    if dry or not url:
        return
    try:
        requests.post(url, headers={"Title": "stocks-updated"}, timeout=15,
                      data=json.dumps({"type": "stocks-updated", "time": int(time.time() * 1000)}))
    except Exception as e:
        print("could not tell the app:", type(e).__name__, flush=True)


def git(*args, check=False):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if p.returncode and check:
        print("git", " ".join(args), "failed:", (p.stderr or p.stdout).strip()[-300:], flush=True)
    return p


def fetch_latest():
    """Other jobs and app updates commit too: start every check from the newest files."""
    git("pull", "--rebase", "--autostash", check=True)


def save_data():
    git("add", "data/")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("nothing changed", flush=True)
        return True
    git("commit", "-m", f"data: stocks {utc_now():%Y-%m-%d %H:%M} UTC", check=True)
    for _ in range(3):
        if git("pull", "--rebase", "--autostash").returncode == 0 and git("push").returncode == 0:
            print("saved data to the repository", flush=True)
            return True
        time.sleep(5)
    print("could not push the data; the next check tries again", flush=True)
    return False


def run_check(dry):
    cmd = [sys.executable, os.path.join(HERE, "run.py")] + (["--dry"] if dry else [])
    return subprocess.run(cmd, cwd=ROOT).returncode


def plan(cfg, start=None, hours=7 * 24):
    """Print the busy hours of the coming week, in Israel time."""
    il = ZoneInfo("Asia/Jerusalem")
    t = (start or utc_now()).replace(minute=0, second=0, microsecond=0)
    state, since = None, None
    for i in range(hours * 4 + 1):
        now = t + dt.timedelta(minutes=15 * i)
        b = busy(now, cfg)
        if b != state:
            if state:
                print(f"  {state:<7} {since.astimezone(il):%a %d %b %H:%M} to {now.astimezone(il):%a %H:%M} Israel time")
            state, since = b, now
    print("  (outside these hours: one check whenever GitHub starts a run)")


def main(argv):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if "--plan" in argv:
        plan(cfg)
        return 0
    dry, once = "--dry" in argv, "--once" in argv
    started = time.time()
    if not dry:
        git("config", "user.name", "stock-checks[bot]")
        git("config", "user.email", "stock-checks@users.noreply.github.com")
    failed, checks, early = False, 0, 0
    settings_url = app_channel("settings")
    seen = None                       # the last app change this run has seen
    tick = None                       # the planned quarter-hour check
    while True:
        if not dry:
            fetch_latest()
        check_began = utc_now()
        before = settings_mark()
        print(f"--- check {checks + 1} at {check_began:%Y-%m-%d %H:%M:%S} UTC", flush=True)
        failed = run_check(dry) != 0
        checks += 1
        if failed:
            print("the check failed; carrying on", flush=True)
        after = settings_mark()
        applied = after != before     # this check applied a change made in the app
        if applied or seen is None:
            seen = after or int(started)
        if not dry:
            if save_data() and applied:
                tell_app()
        now = utc_now()
        why = busy(now, cfg)
        if once:
            break
        if not why:
            print("outside busy hours: one check is enough", flush=True)
            break
        if time.time() - started > MAX_HOURS * 3600:
            print("this run has worked long enough: the next run takes over", flush=True)
            break
        if newer_run_waiting():
            print("a newer run is waiting: handing over to it", flush=True)
            break
        if tick is None or check_began >= tick:           # after an early check the planned one is still owed
            tick = next_tick(now)
        print(f"busy hours ({why}): next check at {tick:%H:%M:%S} UTC, or sooner when the app changes the list", flush=True)
        change = wait_for_change(settings_url if early < MAX_EARLY_CHECKS else None, seen, tick)
        if change:
            early += 1
            seen = change
            print("the list was changed in the app: checking now", flush=True)
            time.sleep(SETTLE_SECONDS)
    print(f"done after {checks} check{'s' if checks != 1 else ''}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
