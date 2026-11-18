"""
Tells the phone that a new version of the app is out.

Runs on GitHub right after index.html is pushed (.github/workflows/announce.yml). When the
app's version number changed, it waits until the website really serves the new version and
then sends one notification of the self-deleting kind: it is not kept in any category of the
app, and it disappears from the phone when the app is opened.

    python worker/announce.py <commit before the push>
"""

import json
import os
import re
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYSTEM_MARK = "?sys=1"                  # a link with this mark tells the app: do not keep this message


def version_of(text):
    m = re.search(r'APP_VERSION = "([^"]+)"', text or "")
    return m.group(1) if m else None


def git(*args):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else None


def whats_new(subject, version):
    """'v4.0: self-deleting messages' -> 'self-deleting messages'."""
    text = re.sub(r"^v?%s\s*[:\-]\s*" % re.escape(version), "", (subject or "").strip())
    text = " ".join(text.split())
    return text[:140]


def message(version, subject):
    new = whats_new(subject, version)
    body = (f"New: {new}.\n" if new else "") + "Open the app to get it."
    return f"Claude News {version} is ready", body


def wait_until_live(base, version, tries=40, pause=10, get=None, sleep=time.sleep):
    """True once the website serves this version (GitHub publishes about a minute after a push)."""
    get = get or requests.get
    for _ in range(tries):
        try:
            r = get(base.rstrip("/") + "/index.html", params={"t": int(time.time())}, timeout=20)
            if version_of(r.text) == version:
                return True
        except Exception:
            pass
        sleep(pause)
    return False


def main(argv):
    before = argv[0] if argv else ""
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        now = version_of(f.read())
    old = version_of(git("show", f"{before}:index.html")) if before and set(before) != {"0"} else None
    print("version before:", old, "now:", now)
    if not now or old is None or old == now:
        print("no new version: nothing to send")
        return 0
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        base = str(json.load(f).get("app_url") or "").strip()
    if not base:
        print("no app address in the settings: nothing sent")
        return 0
    if not wait_until_live(base, now):
        print("the website did not show the new version in time; sending anyway")
    title, body = message(now, git("log", "-1", "--format=%s"))
    sys.path.insert(0, HERE)
    import claude_news_core as cn
    res, _ = cn.notify(title, body, project="Claude News", url=base.rstrip("/") + "/" + SYSTEM_MARK, feed=False, ephemeral=True)
    return 0 if res["pushed"] or not res["devices"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
