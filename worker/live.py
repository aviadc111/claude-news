"""
Candles for the app's candles window, refreshed every minute while the US market is open.

The stock check (run.py) saves its data in the repository every 15 minutes, and the website copy
follows about a minute later. That is too slow and too heavy for one-minute candles. So this
script publishes small files on a branch of their own, `live`, as a single commit that replaces
the one before it (the branch has no history and the website is not rebuilt):

    h/<TICKER>.json     {"t", "at", "m5": [...], "h1": [...]}   5-minute candles of the last 5 trading
                        days and 1-hour candles of the last 60 days; refreshed every 15 minutes
    m/<TICKER>.json     {"t", "at", "m1": [...]}                 the last 120 one-minute candles
    t/<MINUTE>/<TICKER>.json                                     the same m files, under the number of
                        the minute they were published in (minutes since 1970)

A candle is [minute, open, high, low, close]; the minute is its start, in minutes since 1970 (UTC).
Only regular trading hours, 09:30 to 16:00 New York time.

Why the folder named after the minute: GitHub's file server keeps every address for 5 minutes, so
m/<TICKER>.json can be up to 5 minutes old. An address that did not exist a minute ago is served
fresh, but one that is asked for before its file exists answers "not found" for 5 minutes. So
the app never asks early: just after a minute ends it asks for the folder of that minute (the
loop publishes at about second 45 of every minute). Outside market hours it reads m/.

worker/loop.py runs this once a minute during market hours and after every stock check.

    python worker/live.py              download, publish
    python worker/live.py --dry        download and print, publish nothing
    python worker/live.py --out DIR    write the files into DIR instead of publishing (for tests)
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
BRANCH = "live"
KEEP_M1 = 120                          # one-minute candles kept: two hours
HISTORY_MAX_AGE = 14 * 60              # seconds: the 5-minute and 1-hour candles are fetched again after this
CACHE_PATH = os.path.join(tempfile.gettempdir(), "claude_news_live_history.json")
NY = ZoneInfo("America/New_York")
OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60


def symbols():
    """Every stock the app can open a chart of: the list, and the stocks of Claude's demo account."""
    out = []
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), encoding="utf-8") as f:
            out = list(json.load(f).get("tickers") or {})
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(DATA_DIR, "claude_demo.json"), encoding="utf-8") as f:
            out += [h["ticker"] for h in json.load(f).get("holdings", []) if h.get("ticker") not in out]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return out


def price(v):
    return round(float(v), 2 if v >= 1 else 4)


def candles(tickers, period, interval):
    """{ticker: [[minute, open, high, low, close], ...]} from Yahoo, regular hours only."""
    import pandas as pd
    import yfinance as yf
    from run import _table
    data = yf.download(list(tickers), period=period, interval=interval, auto_adjust=True, progress=False,
                       threads=True, prepost=False)
    tables = {f: _table(data, f, list(tickers)) for f in ("Open", "High", "Low", "Close")}
    idx = tables["Close"].index
    if idx.tz is None:
        idx = idx.tz_localize("America/New_York")
    ny = idx.tz_convert(NY)
    minute_of_day = ny.hour * 60 + ny.minute
    keep = (minute_of_day >= OPEN_MIN) & (minute_of_day < CLOSE_MIN)
    minutes = [int(ts.value // 60_000_000_000) for ts in idx]      # .value is nanoseconds whatever the table's unit
    out = {}
    for t in tables["Close"].columns:
        cols = [tables[f][t].values for f in ("Open", "High", "Low", "Close")]
        rows = []
        for k, m in enumerate(minutes):
            if not keep[k]:
                continue
            o, h, l, c = (cols[j][k] for j in range(4))
            if pd.isna(c) or pd.isna(o) or pd.isna(h) or pd.isna(l):
                continue
            o, h, l, c = price(o), price(h), price(l), price(c)
            rows.append([int(m), o, max(h, o, c), min(l, o, c), c])
        if rows:
            out[str(t)] = rows
    return out


def history(tickers, force=False):
    """The 5-minute and 1-hour candles. Kept between runs of this script for 14 minutes."""
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    fresh = time.time() - cache.get("at", 0) < HISTORY_MAX_AGE and all(t in cache.get("m5", {}) for t in tickers)
    if fresh and not force:
        return cache, False
    try:
        new = {"at": time.time(), "m5": candles(tickers, "5d", "5m"), "h1": candles(tickers, "60d", "60m")}
    except Exception as e:
        print("live: no history this time:", type(e).__name__, e, flush=True)
        return cache, False
    if not new["m5"]:
        return cache, False
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(new, f, separators=(",", ":"))
    except OSError:
        pass
    return new, True


def build(tickers, force_history=False):
    """(h files, m files): {name: text}. Raises when the one-minute download fails."""
    m1 = candles(tickers, "2d", "1m")
    if not m1:
        raise RuntimeError("no one-minute prices")
    hist, _ = history(tickers, force_history)
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    dump = lambda d: json.dumps(d, separators=(",", ":"))   # noqa: E731
    h_files, m_files = {}, {}
    for t in tickers:
        if t in m1:
            m_files[t + ".json"] = dump({"t": t, "at": at, "m1": m1[t][-KEEP_M1:]})
        if t in hist.get("m5", {}):
            h_files[t + ".json"] = dump({"t": t, "at": at, "m5": hist["m5"][t], "h1": hist.get("h1", {}).get(t, [])})
    return h_files, m_files


# ----------------------------------------------------------------- publish ---
def git(*args, text_in=None):
    # bytes in and out: in text mode Windows would turn every line end into two characters
    p = subprocess.run(["git", *args], cwd=ROOT, input=None if text_in is None else text_in.encode("utf-8"), capture_output=True)
    if p.returncode:
        raise RuntimeError("git " + args[0] + ": " + (p.stderr or p.stdout).decode("utf-8", "replace").strip()[-300:])
    return p.stdout.decode("utf-8", "replace").strip()


def tree(entries):
    """A git tree out of (mode, kind, id, name) rows."""
    return git("mktree", text_in="".join(f"{mode} {kind} {sha}\t{name}\n" for mode, kind, sha, name in entries))


def files_tree(files):
    return tree([("100644", "blob", git("hash-object", "-w", "--stdin", text_in=text), name)
                 for name, text in sorted(files.items())])


def publish(h_files, m_files, minute=None):
    """One commit with no parent, pushed over the `live` branch. Files that did not change are not sent again."""
    minute = int(time.time() // 60) if minute is None else minute
    m_tree = files_tree(m_files)
    rows = [("040000", "tree", m_tree, "m"), ("040000", "tree", tree([("040000", "tree", m_tree, str(minute))]), "t")]
    if h_files:
        rows.append(("040000", "tree", files_tree(h_files), "h"))
    root = tree(sorted(rows, key=lambda r: r[3]))
    commit = git("-c", "user.name=stock-checks[bot]", "-c", "user.email=stock-checks@users.noreply.github.com",
                 "commit-tree", root, "-m", f"live {minute}")
    git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{BRANCH}")
    return minute


def main(argv):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    tickers = symbols()
    if not tickers:
        print("live: no stocks", flush=True)
        return 0
    began = time.time()
    try:
        h_files, m_files = build(tickers, force_history="--history" in argv)
    except Exception as e:
        print("live: nothing published:", type(e).__name__, e, flush=True)
        return 1
    size = sum(len(x) for x in m_files.values()), sum(len(x) for x in h_files.values())
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
        for folder, files in (("m", m_files), ("h", h_files)):
            os.makedirs(os.path.join(out, folder), exist_ok=True)
            for name, text in files.items():
                with open(os.path.join(out, folder, name), "w", encoding="utf-8") as f:
                    f.write(text)
        print(f"live: wrote {len(m_files)} + {len(h_files)} files to {out} ({size[0]} + {size[1]} bytes)", flush=True)
        return 0
    if "--dry" in argv:
        print(f"live: {len(m_files)} minute files ({size[0]} bytes), {len(h_files)} history files ({size[1]} bytes), not published", flush=True)
        return 0
    try:
        minute = publish(h_files, m_files)
    except Exception as e:
        print("live: could not publish:", type(e).__name__, e, flush=True)
        return 1
    print(f"live: published minute {minute} for {len(m_files)} stocks in {time.time() - began:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
