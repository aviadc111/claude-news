"""Show the WhatsApp login QR that the GitHub job publishes (mode = login-qr).

The job posts the QR text to the Claude News channel "<topic>-qr". This script waits for the
next QR, draws it as a PNG and prints the file's path. Run it on the PC, scan from the phone:
WhatsApp > Linked devices > Link a device.

    python whatsapp/qr_show.py [out.png] [--since ID_OR_TIME] [--wait SECONDS]
"""

import json
import os
import sys
import time

import requests
import segno

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "worker"))
import claude_news_core as cn  # noqa: E402


def main(argv):
    out = next((a for a in argv if not a.startswith("--") and a.endswith(".png")), os.path.join(HERE, "qr.png"))
    since = argv[argv.index("--since") + 1] if "--since" in argv else str(int(time.time()) - 60)
    wait = int(argv[argv.index("--wait") + 1]) if "--wait" in argv else 240
    cfg = cn.load_app_config()
    url = f"{cfg['ntfy'].rstrip('/')}/{cfg['topic']}-qr/json"
    deadline = time.time() + wait
    print("waiting for a QR from the GitHub job...", flush=True)
    while time.time() < deadline:
        try:
            with requests.get(url, params={"since": since}, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if time.time() >= deadline:
                        break
                    if not line:
                        continue
                    m = json.loads(line)
                    if m.get("event") != "message" or not m.get("message"):
                        continue
                    segno.make(m["message"], error="l").save(out, scale=8, border=3)
                    print("QR", m["id"], "saved to", out, flush=True)
                    return 0
        except requests.RequestException as e:
            print("connection hiccup:", type(e).__name__, flush=True)
            time.sleep(3)
    print("no QR arrived in time", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
