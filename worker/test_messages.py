"""
Send a test copy of every message the Stocks tab can receive, right now, from GitHub.
Used to prove the messages arrive with the PC switched off.

  1. the Sunday prices message (current prices of the picked stocks)
  2. the sudden-dip alert (for every picked stock in a sudden dip now, or a sample if none is)
  3. the analyst message (the most recent ratings day that has a stock with enough Buy ratings)

Every title starts with "Test:". Nothing is remembered: the real schedule and its
"already sent" memory are not touched.

    python worker/test_messages.py [--dry]
"""

import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import claude_news_core as cn  # noqa: E402
import run  # noqa: E402
import scans  # noqa: E402


def main(argv):
    dry = "--dry" in argv
    cfg = run.load_json(run.CONFIG_PATH, None)
    app_cfg = cn.load_app_config()
    settings = run.load_settings(cfg)
    devices = None if dry else cn.load_devices()
    sent = []

    def send(title, body, priority="default"):
        title = "Test: " + title
        print("\n--- " + title + " ---\n" + body)
        if not dry:
            res, _ = cn.notify(title, body, project="Stocks", priority=priority, cfg=app_cfg, state=devices)
            sent.append((title, res["pushed"], res["devices"]))

    closes = run.fetch_closes(settings["tickers"])
    results = []
    for ticker, name in settings["tickers"].items():
        if ticker in closes.columns:
            r = run.analyze(ticker, name, closes[ticker], cfg)
            if r:
                results.append(r)
    mine = [r for r in results if r["ticker"] in settings["picked"]]
    if not mine:
        sys.exit("no picked stocks with prices")

    # 1. the weekly prices message
    now = dt.datetime.now(ZoneInfo(cfg["weekly"].get("timezone", "Asia/Jerusalem")))
    send(*run.weekly_message(mine, now))

    # 2. the sudden-dip alert
    sudden = [r for r in mine if r["sudden"]]
    if sudden:
        for r in sudden:
            send(*run.sudden_message(r), priority="high")
    else:
        r = dict(min(mine, key=lambda r: r["chg5_pct"]))
        r["sudden_reasons"] = [f"{r['chg5_pct']:+.1f}% in 5 trading days"]
        title, body = run.sudden_message(r)
        send(title, "(A sample: none of your picked stocks is in a sudden dip right now.)\n" + body, priority="high")

    # 3. the analyst message
    scfg = scans.load_cfg()
    feed, _ = scans.analyst_feed(scfg)
    for day in sorted({row[0] for f in feed for row in f["rows"]}, reverse=True):
        cands = scans.day_candidates(feed, day, scfg)
        if cands:
            hit = scans.add_prices(cands[0])
            hit["sent_before"] = None
            send(*scans.analyst_message(hit))
            break
    else:
        print("\nno ratings day in the feed has a stock with enough Buy ratings - no analyst test message")

    print("\nsent:", sent if not dry else "(dry run, nothing sent)")


if __name__ == "__main__":
    main(sys.argv[1:])
