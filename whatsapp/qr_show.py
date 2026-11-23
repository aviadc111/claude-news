"""Show the WhatsApp login QR that the GitHub job publishes (mode = login-qr).

The job posts the QR text to the Claude News channel "<topic>-qr" (a new one every 45 seconds
while nobody has scanned). This script draws each one as qr.png next to this file and writes
qr.html, a page that reloads the picture every 2 seconds. Serve the folder and open qr.html,
or just open qr.png. Scan from the phone: WhatsApp > Linked devices > Link a device.

    python whatsapp/qr_show.py [--since ID_OR_TIME] [--wait SECONDS] [--once]
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

PNG = os.path.join(HERE, "qr.png")
HTML = os.path.join(HERE, "qr.html")
PAGE = """<!doctype html><meta charset="utf-8"><title>WhatsApp QR</title>
<meta http-equiv="refresh" content="2">
<body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#fff;font-family:sans-serif">
<div style="text-align:center"><img src="qr.png?t=%d" alt="QR" style="width:min(80vw,80vh)">
<p>WhatsApp &gt; Linked devices &gt; Link a device. Updated %s</p></div></body>"""


def main(argv):
    since = argv[argv.index("--since") + 1] if "--since" in argv else str(int(time.time()) - 30)
    wait = int(argv[argv.index("--wait") + 1]) if "--wait" in argv else 900
    once = "--once" in argv
    cfg = cn.load_app_config()
    url = f"{cfg['ntfy'].rstrip('/')}/{cfg['topic']}-qr/json"
    deadline = time.time() + wait
    shown = 0
    print("waiting for a QR from the GitHub job...", flush=True)
    while time.time() < deadline:
        try:
            with requests.get(url, params={"since": since}, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if time.time() >= deadline:
                        return 0 if shown else 1
                    if not line:
                        continue
                    m = json.loads(line)
                    since = m.get("id", since)
                    if m.get("event") != "message" or not m.get("message"):
                        continue
                    segno.make(m["message"], error="l").save(PNG, scale=8, border=3)
                    with open(HTML, "w", encoding="utf-8") as f:
                        f.write(PAGE % (int(time.time()), time.strftime("%H:%M:%S")))
                    shown += 1
                    print(f"QR {shown} ({m['id']}) saved to {PNG}", flush=True)
                    if once:
                        return 0
        except requests.RequestException as e:
            print("connection hiccup:", type(e).__name__, flush=True)
            time.sleep(3)
    print("no QR arrived in time" if not shown else "done", flush=True)
    return 0 if shown else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
