"""
Weekly Instagram follow-back check, run on the server
-----------------------------------------------------
The owner's own account is private. A spare account that the owner opened follows it, and
this job uses the spare account's saved login to read the owner's followers and following
lists, compares them, and sends the result to the Instagram tab of the Claude News app:
who doesn't follow back, who is new on that list, who stopped following.
The owner's own Instagram login is never used here. It stays on the owner's PC.

Rules it keeps:
  * an ordinary, visible browser, no disguise, slow on purpose (a pause between pages);
  * it stops at once when Instagram asks for a login or a verification, or says "too many
    requests", tells the phone, and then stays away: 24 hours, twice as long each time in a
    row, up to a week. It never tries to get around a refusal;
  * no account names are printed (this log is public). The lists are kept in
    data/instagram.enc, encrypted with a key that only the repository's secrets hold.

Environment:
  IG_SPARE_STATE  the spare account's saved login (browser cookies, JSON)
  IG_TARGET       the name of the account to check (the owner's own account)
  DEVICES_KEY     the key that encrypts data/instagram.enc (and the phone list)
  VAPID_PRIVATE_KEY  for the phone notification

    python worker/instagram.py            used by the workflow (under xvfb)
    python worker/instagram.py --dry      reads the lists, prints counts only, sends and saves nothing
    python worker/instagram.py --channel msedge    use an installed browser (for a test on a PC)
"""

import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_ENC = os.path.join(ROOT, "data", "instagram.enc")       # the lists, encrypted
STATE_PATH = os.path.join(ROOT, "data", "instagram_state.json")   # dates only, no names

IG = "https://www.instagram.com/"
APP_ID = "936619743392459"              # the public id the Instagram website itself sends
PAUSE = 4.0                             # seconds between pages of a list: slow on purpose
BREAK_HOURS = 24
CHUNK = 2600                            # characters of names per phone message

ASK = """async ({path, appId}) => {
    try {
        const r = await fetch(path, {headers: {"x-ig-app-id": appId, "x-requested-with": "XMLHttpRequest"}, credentials: "include"});
        const text = await r.text();
        let json = null; try { json = JSON.parse(text); } catch (e) {}
        return {status: r.status, json, url: r.url};
    } catch (e) { return {status: 0, json: null, url: path}; }
}"""


class NeedsLogin(Exception):
    """The spare account's saved login is no longer accepted."""


class Blocked(Exception):
    """Instagram refused: a verification, or too many requests."""


class NotFollowing(Exception):
    """The spare account does not (or no longer) follow the watched account."""


def now():
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------ state on disk ---
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def break_until(state):
    """The end of a running break, or None."""
    try:
        until = dt.datetime.fromisoformat(state["break_until"])
    except (KeyError, TypeError, ValueError):
        return None
    return until if until > now() else None


def start_break(state, reason):
    hits = int(state.get("hits", 0)) + 1
    hours = min(BREAK_HOURS * 2 ** (hits - 1), 24 * 7)
    until = now() + dt.timedelta(hours=hours)
    state.update(break_until=until.isoformat(timespec="minutes"), hits=hits, reason=reason)
    return until


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(os.environ["DEVICES_KEY"].strip().encode())


def load_lists():
    """{account name: {checked, followers, following, not_back}} from the encrypted file."""
    try:
        with open(DATA_ENC, "rb") as f:
            return json.loads(_fernet().decrypt(f.read()).decode("utf-8"))
    except FileNotFoundError:
        return {}


def save_lists(lists):
    with open(DATA_ENC, "wb") as f:
        f.write(_fernet().encrypt(json.dumps(lists).encode("utf-8")))


# ---------------------------------------------------------------- Instagram ---
def refusal(page, res=None):
    """Raise when Instagram is not letting the spare account in."""
    url = page.url
    if "/challenge" in url or "/accounts/suspended" in url:
        raise Blocked("it asked the spare account for a verification")
    if "/accounts/login" in url or (res is not None and "/accounts/login" in (res.get("url") or "")):
        raise NeedsLogin("it asked the spare account to log in again")
    if res is None:
        return
    msg = (res.get("json") or {}).get("message")
    if msg in ("checkpoint_required", "challenge_required"):
        raise Blocked("it asked the spare account for a verification")
    if msg == "login_required" or (res["status"] in (401, 403) and not res.get("json")):
        raise NeedsLogin("it did not accept the spare account's login")
    if res["status"] == 429:
        raise Blocked("it said: too many requests")


def ask(page, path):
    res = page.evaluate(ASK, {"path": path, "appId": APP_ID})
    refusal(page, res)
    return res


def parse_count(text):
    """'1,234' -> (1234, exact)   '12.3K' -> (12300, rounded)"""
    t = text.strip().replace(",", "").replace(" ", "")
    mult = {"k": 1_000, "m": 1_000_000}.get(t[-1:].lower())
    if mult:
        return int(float(t[:-1]) * mult), False
    return int(float(t)), True


def read_list(page, uid, kind):
    """All names in the followers or following list, page by page."""
    names, max_id, pages = [], "", 0
    per_page = 25 if kind == "followers" else 50
    while True:
        res = ask(page, f"/api/v1/friendships/{uid}/{kind}/?count={per_page}" + (f"&max_id={max_id}" if max_id else ""))
        if res["status"] != 200 or not res["json"]:
            raise Blocked(f"reading the {kind} list failed (answer {res['status']})")
        users = res["json"].get("users") or []
        names += [u["username"].lower() for u in users if u.get("username")]
        pages += 1
        if pages % 5 == 0:
            print(f"  {kind}: {pages} pages so far", flush=True)
        max_id = res["json"].get("next_max_id")
        if not max_id or not users or pages > 4000:
            break
        time.sleep(PAUSE)
    return sorted(set(names))


def collect(state, target, channel=None):
    """Open the watched account's page with the spare login and read both lists."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel=channel) if channel else p.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(storage_state=state, locale="en-US", viewport={"width": 1100, "height": 900})
            page = ctx.new_page()
            page.goto(IG + target + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            refusal(page)
            cookies = {c["name"]: c["value"] for c in ctx.cookies(IG)}
            if not (cookies.get("sessionid") and cookies.get("ds_user_id")):
                raise NeedsLogin("the spare account's login was not kept")
            html = page.content()
            ids = [i for i in re.findall(r'"profile_id":"(\d+)"', html) + re.findall(r"profilePage_(\d+)", html)
                   if i != cookies.get("ds_user_id")]
            if not ids:
                raise Blocked("the account's page did not show which account it is")
            uid = max(set(ids), key=ids.count)
            counts = {"followers": None, "following": None, "exact": True}
            desc = page.evaluate("""() => { const m = document.querySelector('meta[property="og:description"], meta[name="description"]'); return m ? m.content : ""; }""")
            m = re.search(r"([\d.,]+\s?[KkMm]?)\s+Followers?,\s*([\d.,]+\s?[KkMm]?)\s+Following", desc or "")
            if m:
                f, e1 = parse_count(m.group(1))
                g, e2 = parse_count(m.group(2))
                counts = {"followers": f, "following": g, "exact": e1 and e2}
            print("profile page: opened, logged in, totals " + ("read" if m else "not shown"), flush=True)

            time.sleep(PAUSE)
            fr = ask(page, f"/api/v1/friendships/show/{uid}/").get("json") or {}
            if fr.get("is_private") and not fr.get("following"):
                raise NotFollowing("the follow request is still waiting to be accepted" if fr.get("outgoing_request")
                                   else "the spare account does not follow the account")
            time.sleep(PAUSE)
            following = read_list(page, uid, "following")
            time.sleep(PAUSE)
            followers = read_list(page, uid, "followers")
            return {"counts": counts, "followers": followers, "following": following}
        finally:
            browser.close()


# ------------------------------------------------------------------ report ---
def complete(got, expected, exact=True):
    """Instagram's counters include a few deactivated accounts, so allow a small gap.
    Large totals are shown rounded (12.3K), so those get a wider one."""
    if expected is None:
        return True
    return got >= expected * (0.97 if exact else 0.93) - 2


def build_messages(data, last):
    followers, following = set(data["followers"]), set(data["following"])
    not_back = sorted(following - followers)
    prev = set(last["not_back"]) if last else None
    new = sorted(set(not_back) - prev) if prev is not None else []
    gone = len(prev - set(not_back)) if prev is not None else 0
    lost = sorted(set(last["followers"]) - followers) if last and last.get("followers") else []

    n = len(not_back)
    title = f"{n} {'person doesn' + chr(39) + 't' if n == 1 else 'people don' + chr(39) + 't'} follow you back"
    head = [f"You follow {len(following):,}. {len(followers):,} follow you."]
    if prev is not None:
        head.append(f"Since the last check: {len(new)} new on this list, {gone} no longer on it.")
        if new:
            head.append("New: " + ", ".join("@" + u for u in new[:40]) + (" ..." if len(new) > 40 else ""))
        if lost:
            head.append(f"Stopped following you ({len(lost)}): " + ", ".join("@" + u for u in lost[:40]) + (" ..." if len(lost) > 40 else ""))
    chunks, cur = [], ""
    for u in not_back:
        line = "@" + u + "\n"
        if len(cur) + len(line) > CHUNK:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    messages = []
    if not chunks:
        messages.append((title, "\n".join(head) + "\n\nEveryone you follow follows you back."))
    for i, chunk in enumerate(chunks):
        if i == 0:
            messages.append((title, "\n".join(head) + "\n\nWho they are:\n" + chunk.rstrip()))
        else:
            messages.append((f"Who doesn't follow you back ({i + 1}/{len(chunks)})", chunk.rstrip()))
    return messages, not_back


def when_text(d):
    try:
        from zoneinfo import ZoneInfo
        d = d.astimezone(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        pass
    return d.strftime("%A %d %b at %H:%M")


class Phone:
    """Sends to the app; in a dry run it only counts."""

    def __init__(self, dry):
        self.dry, self.sent, self.devices, self.changed = dry, [], None, False

    def send(self, title, body, priority="default"):
        self.sent.append(title)
        if self.dry:
            return
        sys.path.insert(0, HERE)
        import claude_news_core as cn
        if self.devices is None:
            self.devices = cn.load_devices()
        _, ch = cn.notify(title, body, project="Instagram", priority=priority, state=self.devices)
        self.changed |= ch

    def close(self):
        if self.changed and not self.dry:
            import claude_news_core as cn
            cn.save_devices(self.devices)


def run(dry=False, channel=None, collect_fn=None):
    raw, target = os.environ.get("IG_SPARE_STATE", ""), os.environ.get("IG_TARGET", "").strip().lstrip("@").lower()
    if not raw or not target:
        print("IG_SPARE_STATE or IG_TARGET is not set: nothing to check")
        return 2
    try:
        login = json.loads(raw)
    except ValueError:
        print("IG_SPARE_STATE is not valid")
        return 2
    state, phone = load_state(), Phone(dry)
    try:
        until = break_until(state)
        if until:
            print("skipped: on a break", flush=True)
            phone.send("Instagram check skipped",
                       f"Instagram stopped an earlier check ({state.get('reason', 'no reason given')}). To protect the spare "
                       f"account, no check runs until {when_text(until)}. After that the weekly check works again.")
            return 5
        print("check started" + (" (dry run)" if dry else ""), flush=True)
        state["last_try"] = now().isoformat(timespec="minutes")
        try:
            data = (collect_fn or collect)(login, target, channel)
        except NeedsLogin as e:
            print("stopped: the spare login is not accepted", flush=True)
            phone.send("Instagram check: the spare account must log in again",
                       f"Instagram did not let the spare account in ({e}).\n\n"
                       "On the PC, in the InstagramCheck folder: double-click \"Log in to the SPARE Instagram account\", "
                       "log in, then double-click \"Copy the spare login to GitHub\". Your own account is not affected.",
                       priority="high")
            return 2
        except NotFollowing as e:
            print("stopped: the spare account does not follow the watched account", flush=True)
            phone.send("Instagram check: the spare account does not follow you",
                       f"The check could not read your lists: {e}.\n\n"
                       "From the spare account, follow your account again, and accept the request.")
            return 4
        except Blocked as e:
            print("stopped by Instagram", flush=True)
            until = None if dry else start_break(state, str(e))
            pause = (f"To protect the spare account the check now takes a break: no check runs until {when_text(until)}. "
                     "After that the weekly check works again.\n" if until else "")
            phone.send("Instagram check stopped",
                       f"Instagram did not allow the check this time ({e}). Nothing was sent or changed.\n{pause}\n"
                       "This is about the spare account only. Your own account was not used.", priority="high")
            return 3

        counts = data["counts"]
        ok = (complete(len(data["followers"]), counts["followers"], counts["exact"])
              and complete(len(data["following"]), counts["following"], counts["exact"]))
        print("lists read: " + ("complete" if ok else "INCOMPLETE") + (", totals unknown" if counts["followers"] is None else ""), flush=True)
        if not ok:
            phone.send("Instagram check: lists came back incomplete",
                       f"Instagram returned {len(data['followers'])} of about {counts['followers']} followers and "
                       f"{len(data['following'])} of about {counts['following']} following, so the comparison would be wrong. "
                       "Nothing was saved. The next weekly check tries again.")
            return 6
        lists = {} if dry and not os.environ.get("DEVICES_KEY") else load_lists()
        messages, not_back = build_messages(data, lists.get(target))
        if counts["followers"] is None:
            title, body = messages[0]
            messages[0] = (title, body + "\n\n(The totals on your profile could not be read this time, "
                                         "so the lists could not be checked for completeness.)")
        for title, body in messages:
            phone.send(title, body)
        print(f"report sent in {len(messages)} message{'s' if len(messages) != 1 else ''}", flush=True)
        if not dry:
            lists[target] = {"checked": now().isoformat(timespec="seconds"), "followers": data["followers"],
                             "following": data["following"], "not_back": not_back}
            save_lists(lists)
            state.update(last_ok=now().isoformat(timespec="minutes"), hits=0, break_until=None, reason=None)
        return 0
    finally:
        phone.close()
        if not dry:
            save_state(state)


def main(argv):
    channel = argv[argv.index("--channel") + 1] if "--channel" in argv else None
    return run("--dry" in argv, channel)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
