"""
Claude News sender used by the GitHub job (no PC needed).

Secrets come from the environment:
  VAPID_PRIVATE_KEY  the PEM text of vapid_private.pem
  DEVICES_KEY        Fernet key that encrypts the phone list in data/devices.enc

The phone list is kept encrypted in the public repository so it survives between
runs without exposing anyone's push address.
"""

import datetime as dt
import json
import os
import tempfile

import requests
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DEVICES_ENC = os.path.join(DATA_DIR, "devices.enc")
APP_CONFIG = os.path.join(ROOT, "config.js")


def load_app_config():
    """ntfy server, topic and public key, read from the app's own config.js."""
    with open(APP_CONFIG, encoding="utf-8") as f:
        text = f.read()
    start, end = text.index("{"), text.rindex("}") + 1
    return json.loads(text[start:end])


def _fernet():
    key = os.environ.get("DEVICES_KEY")
    if not key:
        raise RuntimeError("DEVICES_KEY is not set")
    return Fernet(key.encode())


def load_devices():
    if not os.path.exists(DEVICES_ENC):
        return {"since": None, "devices": {}}
    with open(DEVICES_ENC, "rb") as f:
        return json.loads(_fernet().decrypt(f.read()).decode("utf-8"))


def save_devices(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEVICES_ENC, "wb") as f:
        f.write(_fernet().encrypt(json.dumps(state).encode("utf-8")))


def refresh_devices(cfg, state):
    """Merge new phone registrations from ntfy into the state. Returns True if changed."""
    since = state.get("since") or "all"
    url = f"{cfg['ntfy'].rstrip('/')}/{cfg['topic']}-devices/json?poll=1&since={since}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("could not check for new devices:", e)
        return False
    changed = False
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        if m.get("id") and m["id"] != state.get("since"):
            state["since"] = m["id"]
            changed = True
        try:
            data = json.loads(m.get("message", ""))
        except ValueError:
            continue
        sub = data.get("sub") or {}
        endpoint = sub.get("endpoint")
        if not endpoint:
            continue
        if data.get("action") == "unsubscribe":
            if state["devices"].pop(endpoint, None):
                print("device removed:", data.get("name"))
            continue
        if endpoint not in state["devices"]:
            print("new device:", data.get("name"))
        state["devices"][endpoint] = {
            "sub": sub, "name": data.get("name", "phone"),
            "added": dt.datetime.fromtimestamp(m.get("time", 0)).isoformat(timespec="seconds"),
        }
    return changed


def _vapid_key_path():
    pem = os.environ.get("VAPID_PRIVATE_KEY")
    if not pem:
        raise RuntimeError("VAPID_PRIVATE_KEY is not set")
    path = os.path.join(tempfile.gettempdir(), "vapid_private.pem")
    with open(path, "w", encoding="utf-8") as f:
        f.write(pem.strip() + "\n")
    return path


def notify(title, body, project="General", url=None, priority="default", cfg=None, state=None):
    """Publish to the feed and push to every phone. Returns (summary dict, devices changed)."""
    from pywebpush import WebPushException, webpush

    cfg = cfg or load_app_config()
    state = state if state is not None else load_devices()
    changed = refresh_devices(cfg, state)
    result = {"feed": False, "pushed": 0, "failed": 0, "devices": len(state["devices"])}

    headers = {"Title": title, "Priority": priority, "Tags": project.replace(",", " ")}
    if url:
        headers["Click"] = url
    try:
        r = requests.post(f"{cfg['ntfy'].rstrip('/')}/{cfg['topic']}", data=body.encode("utf-8"),
                          headers=headers, timeout=20)
        r.raise_for_status()
        result["feed"] = True
    except Exception as e:
        print("ntfy publish failed:", e)

    if state["devices"]:
        key_path = _vapid_key_path()
        payload = json.dumps({"title": title, "body": body, "project": project, "url": url or "",
                              "time": dt.datetime.now().isoformat(timespec="seconds")})
        for endpoint, dev in list(state["devices"].items()):
            try:
                webpush(subscription_info=dev["sub"], data=payload, vapid_private_key=key_path,
                        vapid_claims={"sub": "mailto:claude-news@example.com"}, ttl=24 * 3600,
                        headers={"Urgency": "high" if priority in ("high", "urgent") else "normal"})
                result["pushed"] += 1
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status in (404, 410):
                    state["devices"].pop(endpoint, None)
                    changed = True
                    print("device gone, removed:", dev.get("name"))
                else:
                    result["failed"] += 1
                    print("push failed for", dev.get("name"), e)
            except Exception as e:
                result["failed"] += 1
                print("push failed for", dev.get("name"), e)
    print(f"sent [{project}] {title} -> feed={result['feed']} pushed={result['pushed']}/{result['devices']}")
    return result, changed
