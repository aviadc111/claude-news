"""
One small test: can the spare Instagram account be used from this server?

Background: the follow-back check normally runs on the owner's PC. This tries the idea of a
spare account that follows the owner's account, so the check could run here while the PC is off.

What it does: opens the watched account's profile page once, in an ordinary, visible browser
with the spare account's saved login, asks whether the spare account follows it, and (only
if it does) asks for the first page of its followers list. Then it tells the phone what
happened. Nothing else.

Rules it keeps:
  * no disguise of any kind: a normal browser, slow, a handful of requests;
  * it stops at once if Instagram asks for a login or a verification, and says so;
  * no account names are printed (this log is public). The result goes to the phone only.

Environment:
  IG_SPARE_STATE  the spare account's saved login (browser cookies, JSON)
  IG_TARGET       the name of the account to look at (the owner's own account)

    python worker/instagram_probe.py            used by the workflow (under xvfb)
    python worker/instagram_probe.py --dry      prints the result, sends nothing
    python worker/instagram_probe.py --channel msedge    use an installed browser (for a test on the PC)
"""

import json
import os
import re
import sys
import time

IG = "https://www.instagram.com/"
APP_ID = "936619743392459"              # the public id the Instagram website itself sends
PAUSE = 4.0

ASK = """async ({path, appId}) => {
    try {
        const r = await fetch(path, {headers: {"x-ig-app-id": appId, "x-requested-with": "XMLHttpRequest"}, credentials: "include"});
        const text = await r.text();
        let json = null; try { json = JSON.parse(text); } catch (e) {}
        return {status: r.status, json, url: r.url};
    } catch (e) { return {status: 0, json: null, url: path}; }
}"""


def refused(page, res=None):
    """Why Instagram is not letting us in, or '' when it is."""
    url = page.url
    if "/challenge" in url or "/accounts/suspended" in url:
        return "it asked the spare account for a verification"
    if "/accounts/login" in url:
        return "it asked the spare account to log in again"
    if res is not None:
        if "/accounts/login" in (res.get("url") or ""):
            return "it asked the spare account to log in again"
        msg = (res.get("json") or {}).get("message")
        if msg in ("checkpoint_required", "challenge_required"):
            return "it asked the spare account for a verification"
        if msg == "login_required" or res["status"] in (401, 403):
            return "it did not accept the spare account's login"
        if res["status"] == 429:
            return "it said: too many requests"
    return ""


def probe(state, target, channel=None):
    """Returns a dict: ok (bool), step, why, and what was seen."""
    from playwright.sync_api import sync_playwright
    out = {"ok": False, "step": "open", "why": "", "private": None, "follows": None, "followers": None,
           "following": None, "list_page": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel=channel) if channel else p.chromium.launch(headless=False)
        try:
            ctx = browser.new_context(storage_state=state, locale="en-US", viewport={"width": 1100, "height": 900})
            page = ctx.new_page()
            # 1) the watched account's profile page, opened the way a person would
            page.goto(IG + target + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            cookies = {c["name"]: c["value"] for c in ctx.cookies(IG)}
            why = refused(page)
            if not why and not (cookies.get("sessionid") and cookies.get("ds_user_id")):
                why = "the spare account's login was not kept"
            if why:
                out["why"] = why
                return out
            print("profile page: opened, logged in", flush=True)

            out["step"] = "profile"
            html = page.content()
            ids = [i for i in re.findall(r'"profile_id":"(\d+)"', html) + re.findall(r"profilePage_(\d+)", html)
                   if i != cookies.get("ds_user_id")]
            if not ids:
                out["why"] = "the account's page did not show which account it is (the page may have changed)"
                return out
            uid = max(set(ids), key=ids.count)
            desc = page.evaluate("""() => { const m = document.querySelector('meta[property="og:description"], meta[name="description"]'); return m ? m.content : ""; }""")
            m = re.search(r"([\d.,]+\s?[KkMm]?)\s+Followers?,\s*([\d.,]+\s?[KkMm]?)\s+Following", desc or "")
            if m:
                out["followers"], out["following"] = m.group(1), m.group(2)

            # 2) does the spare account follow it?
            time.sleep(PAUSE)
            res = page.evaluate(ASK, {"path": f"/api/v1/friendships/show/{uid}/", "appId": APP_ID})
            why = refused(page, res)
            fr = res.get("json") if not why else None
            if why or res["status"] != 200 or not isinstance(fr, dict):
                out["why"] = why or f"the follow state could not be read (answer {res['status']})"
                return out
            out["private"], out["follows"] = bool(fr.get("is_private")), bool(fr.get("following"))
            print(f"profile: readable, private: {out['private']}, the spare account follows it: {out['follows']}", flush=True)
            if out["private"] and not out["follows"]:
                out["why"] = ("the follow request is still waiting to be accepted" if fr.get("outgoing_request")
                              else "the spare account does not follow the account yet")
                return out

            # 3) the first page of the followers list
            out["step"] = "list"
            time.sleep(PAUSE)
            res = page.evaluate(ASK, {"path": f"/api/v1/friendships/{uid}/followers/?count=12", "appId": APP_ID})
            why = refused(page, res)
            users = (res.get("json") or {}).get("users") if not why else None
            if why or res["status"] != 200 or users is None:
                out["why"] = why or f"the followers list could not be read (answer {res['status']})"
                return out
            out["list_page"] = len(users)
            print(f"followers list: first page readable ({'some' if users else 'no'} names)", flush=True)
            out["ok"] = True
            out["step"] = "done"
            return out
        finally:
            browser.close()


def message(out):
    if out["ok"]:
        return ("Server test: it works",
                "The spare account was accepted from the server and your lists can be read there.\n"
                f"Your profile shows {out['followers']} followers and {out['following']} following; "
                f"the first page of the followers list came back with {out['list_page']} names.\n\n"
                "Next: the weekly check can be built to run on the server.")
    where = {"open": "when opening your profile", "profile": "when looking at your profile",
             "list": "when reading the followers list"}.get(out["step"], "")
    return ("Server test: not yet",
            f"The test stopped {where}: {out['why']}.\n\n"
            "Nothing was changed. Your own account was not used.")


def main(argv):
    dry = "--dry" in argv
    channel = argv[argv.index("--channel") + 1] if "--channel" in argv else None
    raw, target = os.environ.get("IG_SPARE_STATE", ""), os.environ.get("IG_TARGET", "").strip().lstrip("@")
    if not raw or not target:
        print("IG_SPARE_STATE or IG_TARGET is not set: nothing to test")
        return 2
    try:
        state = json.loads(raw)
    except ValueError:
        print("IG_SPARE_STATE is not valid")
        return 2
    try:
        out = probe(state, target, channel)
    except Exception as e:                                   # a crash is also an answer; no details that could name anyone
        out = {"ok": False, "step": "open", "why": "the browser could not finish (" + type(e).__name__ + ")",
               "followers": None, "following": None, "list_page": None}
    print("result:", "works" if out["ok"] else f"stopped at '{out['step']}': {out['why']}", flush=True)
    title, body = message(out)
    if dry:
        print("--- message for the phone (not sent) ---")
        print(title)
        print(body)
        return 0
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import claude_news_core as cn
    res, _ = cn.notify(title, body, project="Instagram", priority="default")
    print("phone told:", bool(res["feed"] or res["pushed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
