"""
Market-wide scans that run on GitHub.

  analysts   For each day of analyst ratings, once: which large company did the most
             analyst firms rate "Buy" that day? If one got `analyst_min_buy_ratings`
             or more Buy-type ratings from different firms, send it: name, price,
             all-time high and 30-day high / low. At most one stock per ratings day.

             The ratings feed reaches GitHub's servers a day or more late, so the scan
             does not ask "what happened today". It looks at the last week, and handles
             every ratings day it has not handled yet as soon as that day shows up.

             Repeat rule: a stock that was sent is not sent again for
             `analyst_repeat_days` days. After that it is sent again only if it gets
             many Buy ratings again AND it is not one of the picked stocks.

             These are analysts' published ratings, reported as they are. Not advice.

  friday     SWITCHED OFF (`friday_scan_enabled` in config.json; still runnable by hand).
             Every US-listed stock (not only the picked ones) that had a sudden dip -
             down `sudden_drop_day_pct` today or `sudden_drop_5day_pct` in 5 trading
             days - and that either traded at `friday_recent_high` dollars or more in
             the last 3 months, or once cost `friday_alltime_high` dollars or more.

    python worker/scans.py auto               what the schedule runs (the analysts check)
    python worker/scans.py analysts|friday|both [--dry] [--force] [--from YYYY-MM-DD]
"""

import datetime as dt
import json
import os
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(HERE, "config.json")
SCANS_PATH = os.path.join(DATA_DIR, "scans.json")
TICKERS_PATH = os.path.join(DATA_DIR, "tickers.txt")
sys.path.insert(0, HERE)

EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]          # NASDAQ, NYSE, NYSE American
NY = ZoneInfo("America/New_York")
BUY_GRADES = {"buy", "strong buy", "outperform", "overweight", "positive", "accumulate", "add",
              "market outperform", "sector outperform", "top pick", "conviction buy",
              "long-term buy", "speculative buy", "moderate buy", "outperformer"}
DEFAULTS = {
    "analyst_min_buy_ratings": 5,
    "analyst_min_market_cap": 10_000_000_000,
    "analyst_repeat_days": 14,
    "analyst_memory_days": 60,
    "friday_scan_enabled": False,
    "friday_min_price": 5.0,
    "friday_recent_high": 100.0,
    "friday_alltime_high": 120.0,
    "friday_list_max": 15,
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def load_cfg():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def nice_names():
    """SYMBOL -> clean company name, from the app's ticker list."""
    names = {}
    if os.path.exists(TICKERS_PATH):
        with open(TICKERS_PATH, encoding="utf-8") as f:
            for line in f:
                if line and line[0] != "#" and "|" in line:
                    sym, name = line.rstrip("\n").split("|", 1)
                    names[sym] = name
    return names


def tidy(name):
    name = " ".join(str(name or "").split())
    for _ in range(2):
        name = re.sub(r",?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Ltd\.?|Limited|PLC|plc|N\.V\.|S\.A\.|"
                      r"Co\.|Company|Holdings?|Group)$", "", name).strip(" ,")
    return name


def nice_day(iso):
    return dt.date.fromisoformat(iso).strftime("%A %d %b")


# ---------------------------------------------------------------- screener ---
def screen_all(query):
    import yfinance as yf
    out, off = [], 0
    while True:
        res = yf.screen(query, size=250, offset=off, sortField="intradaymarketcap", sortAsc=False)
        quotes = res.get("quotes", [])
        out += quotes
        if len(quotes) < 250 or off > 20000:
            break
        off += 250
    # ordinary shares only: no preferreds (JPM-PC), units or warrants
    return [q for q in out if q.get("quoteType") == "EQUITY" and not re.search(r"-P[A-Z]?$|-U$|-WS?$|-R$", q["symbol"])]


def universe(extra):
    import yfinance as yf
    Q = yf.EquityQuery
    return screen_all(Q("and", [Q("eq", ["region", "us"]), Q("is-in", ["exchange"] + EXCHANGES), extra]))


def closes(symbols, period, batch=400):
    """Split-adjusted (not dividend-adjusted) daily closes: the prices people actually paid."""
    import pandas as pd
    import yfinance as yf
    frames = []
    symbols = list(symbols)
    for i in range(0, len(symbols), batch):
        part = symbols[i:i + batch]
        try:
            d = yf.download(part, period=period, interval="1d", auto_adjust=False, progress=False, threads=True)["Close"]
            if isinstance(d, pd.Series):
                d = d.to_frame(name=part[0])
            frames.append(d)
        except Exception as e:
            print("download failed for a batch:", type(e).__name__, e)
        if len(symbols) > batch:
            print(f"  prices {min(i + batch, len(symbols))}/{len(symbols)}", flush=True)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out.loc[:, ~out.columns.duplicated()]


# ------------------------------------------------------------------ analysts ---
def analyst_feed(cfg, days=7):
    """Recent rating actions for every large company.
    Returns (feed, newest day in the feed). feed = [{ticker, name, rows}], and each row is
    (ratings day in New York, firm, grade, action, price-target action), newest first."""
    import pandas as pd
    import yfinance as yf
    Q = yf.EquityQuery
    quotes = [q for q in universe(Q("gt", ["intradaymarketcap", int(cfg["analyst_min_market_cap"])]))
              if q.get("averageAnalystRating")]
    since = pd.Timestamp(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).tz_localize(None)
    newest = []

    def fetch(q):
        sym = q["symbol"]
        try:
            df = yf.Ticker(sym).upgrades_downgrades
            if df is None or df.empty:
                return "empty"
            if df.index.tz is not None:
                df.index = df.index.tz_convert("UTC").tz_localize(None)
            newest.append(df.index.max())
            df = df[df.index >= since]
            if df.empty:
                return None
            ny_days = [t.date().isoformat() for t in df.index.tz_localize("UTC").tz_convert(NY)]
            target = df["priceTargetAction"] if "priceTargetAction" in df.columns else [""] * len(df)
            rows = [(d, str(f), str(g).strip().lower(), str(a).lower(), str(p).lower())
                    for d, f, g, a, p in zip(ny_days, df["Firm"], df["ToGrade"], df["Action"], target)]
            return {"ticker": sym, "name": q.get("shortName") or sym, "rows": rows}
        except Exception:
            return "error"

    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(fetch, quotes))
    failed = [i for i, r in enumerate(res) if r == "error"]
    if failed and len(failed) < len(quotes) * 0.5:       # a few hiccups: wait and try those once more
        import time
        time.sleep(20)
        for i in failed:
            res[i] = fetch(quotes[i])
            time.sleep(0.3)
    errors, empty = sum(1 for r in res if r == "error"), sum(1 for r in res if r == "empty")
    feed = [r for r in res if isinstance(r, dict)]
    newest_ts = max(newest) if newest else None
    newest_day = newest_ts.tz_localize("UTC").tz_convert(NY).date().isoformat() if newest_ts is not None else None
    print(f"yfinance {yf.__version__}, pandas {pd.__version__} | companies checked: {len(quotes)} | "
          f"lookups failed: {errors} | no ratings history: {empty}")
    print(f"newest rating in the feed: {newest_ts} UTC (ratings day {newest_day})")
    if not quotes or empty + errors > len(quotes) * 0.15:
        raise RuntimeError(f"analyst data missing for {empty + errors} of {len(quotes)} companies - "
                           f"not trusting this run; the next run tries again")
    return feed, newest_day


def day_candidates(feed, day, cfg):
    """Stocks with enough Buy-type ratings from different firms on one ratings day, best first."""
    out = []
    for f in feed:
        seen, buy, other, upgrades, raised, lowered = set(), set(), set(), set(), set(), set()
        for d, firm, grade, action, target in f["rows"]:
            if d != day or firm in seen:                    # newest action per firm that day
                continue
            seen.add(firm)
            if grade in BUY_GRADES:
                buy.add(firm)
                if action in ("up", "init"):
                    upgrades.add(firm)
                if target.startswith("raise"):
                    raised.add(firm)
                elif target.startswith("lower"):
                    lowered.add(firm)
            elif grade:
                other.add(firm)
        if len(buy) >= int(cfg["analyst_min_buy_ratings"]):
            out.append({"ticker": f["ticker"], "name": f["name"], "day": day, "buy": len(buy), "upgrades": len(upgrades),
                        "other": len(other), "raised": len(raised), "lowered": len(lowered), "firms": sorted(buy)[:12]})
    return sorted(out, key=lambda r: (-r["buy"], -r["upgrades"], r["other"]))


def add_prices(hit):
    full = closes([hit["ticker"]], "max")
    s = full[hit["ticker"]].dropna() if hit["ticker"] in full.columns else None
    if s is not None and len(s) > 1:
        w = s.iloc[-30:]
        hit.update(price=round(float(s.iloc[-1]), 2), ath=round(float(s.max()), 2), ath_date=str(s.idxmax().date()),
                   high30=round(float(w.max()), 2), low30=round(float(w.min()), 2))
    hit["name"] = nice_names().get(hit["ticker"]) or tidy(hit["name"])
    return hit


def analyst_message(r):
    day = nice_day(r["day"])
    title = f"Most analyst buy ratings: {r['name']} ({r['ticker']})"
    up = f", {r['upgrades']} of them new or upgraded" if r["upgrades"] else ""
    neg = f" {r['other']} rated it Hold or lower." if r["other"] else ""
    lines = [f"{r['buy']} analyst firms rated {r['name']} a Buy on {day}{up}.{neg}"]
    if r.get("raised") or r.get("lowered"):
        lines.append(f"Their price targets: {r.get('raised', 0)} raised, {r.get('lowered', 0)} lowered.")
    if r.get("sent_before"):
        lines.append(f"Sent to you before on {nice_day(r['sent_before'])} and it is getting Buy ratings again. "
                     f"It is not one of your picked stocks.")
    if "price" in r:
        lines += [f"Now ${r['price']:,.2f}", f"All-time high ${r['ath']:,.2f} ({r['ath_date']})",
                  f"30-day high ${r['high30']:,.2f}", f"30-day low ${r['low30']:,.2f}"]
    return title, "\n".join(lines) + "\n\nThese are analysts' published ratings, not advice."


def run_analysts(cfg, state, argv, send, now_ny):
    """Handle every ratings day that is new since the last run. Returns True if state changed."""
    today = now_ny.date()
    feed, newest_day = analyst_feed(cfg)
    if not newest_day:
        print("the ratings feed is empty - nothing to do")
        return False
    days = state.setdefault("analyst_days", {})       # ratings day -> ticker sent ("" = nothing qualified)
    sent = state.setdefault("analyst_sent", {})       # ticker -> the day I sent it
    if "--from" in argv:
        start = argv[argv.index("--from") + 1]
    else:
        # the first run only notes where the feed stands; days already in it are not replayed
        start = state.setdefault("analyst_start", newest_day)
    picked = set(load_json(os.path.join(DATA_DIR, "settings.json"), {}).get("picked", []))
    repeat_days = int(cfg["analyst_repeat_days"])

    def age(ticker):
        return (today - dt.date.fromisoformat(sent[ticker])).days if ticker in sent else None

    def skip(ticker):
        a = age(ticker)
        if a is None:
            return False                               # never sent: send it
        if a < repeat_days:
            return True                                # sent in the last two weeks: stay quiet
        return ticker in picked                        # later: again only if it is not a picked stock

    after_close = (now_ny.hour, now_ny.minute) >= (16, 30)
    for day in sorted({row[0] for f in feed for row in f["rows"]}):
        if day <= start or (day in days and "--force" not in argv):
            continue
        if day == today.isoformat() and not after_close:
            continue                                   # today's ratings are still coming in
        cands = day_candidates(feed, day, cfg)
        print(f"ratings day {day}: {[(c['ticker'], c['buy']) for c in cands[:6]] or 'no stock reached the bar'}")
        quiet = [c["ticker"] for c in cands if skip(c["ticker"])]
        if quiet:
            print("  not repeated (sent recently, or already a picked stock):", quiet)
        pick = next((c for c in cands if not skip(c["ticker"])), None)
        if pick:
            pick["sent_before"] = sent.get(pick["ticker"])
            if send(*analyst_message(add_prices(pick))):
                days[day] = pick["ticker"]
                sent[pick["ticker"]] = today.isoformat()
        elif (today - dt.date.fromisoformat(day)).days >= 2:
            days[day] = ""                             # settled: nothing worth sending that day
    for key in [d for d in days if (today - dt.date.fromisoformat(d)).days > 30]:
        del days[key]
    for key in [t for t in sent if age(t) > int(cfg["analyst_memory_days"])]:
        del sent[key]
    state.pop("analysts", None)                        # old format
    return True


# ------------------------------------------------------------ friday (weekly) ---
def friday_scan(cfg):
    import yfinance as yf
    Q = yf.EquityQuery
    quotes = universe(Q("gt", ["intradayprice", float(cfg["friday_min_price"])]))
    meta = {q["symbol"]: q for q in quotes}
    print("stocks to check:", len(meta))
    px = closes(meta, "3mo")
    day_lim, wk_lim = -float(cfg["sudden_drop_day_pct"]), -float(cfg["sudden_drop_5day_pct"])
    last_day = px.dropna(how="all").index[-1]
    dips = []
    for sym in px.columns:
        s = px[sym].dropna()
        if len(s) < 7 or s.index[-1] != last_day:          # stale or newly listed
            continue
        last = float(s.iloc[-1])
        day = (last / float(s.iloc[-2]) - 1) * 100
        wk = (last / float(s.iloc[-6]) - 1) * 100
        if day <= day_lim or wk <= wk_lim:
            dips.append({"ticker": sym, "price": round(last, 2), "day_pct": round(day, 1), "wk_pct": round(wk, 1),
                         "high3m": round(float(s.max()), 2), "cap": meta[sym].get("marketCap") or 0,
                         "name": meta[sym].get("shortName") or sym})
    print("sudden dips before the price rule:", len(dips))
    keep = [d for d in dips if d["high3m"] >= float(cfg["friday_recent_high"])]
    rest = [d for d in dips if d["high3m"] < float(cfg["friday_recent_high"])]
    if rest:                                               # did it ever cost friday_alltime_high or more?
        full = closes([d["ticker"] for d in rest], "max")
        for d in rest:
            s = full[d["ticker"]].dropna() if d["ticker"] in full.columns else None
            if s is not None and len(s) and float(s.max()) >= float(cfg["friday_alltime_high"]):
                d["ath"] = round(float(s.max()), 2)
                keep.append(d)
    names = nice_names()
    for d in keep:
        d["name"] = names.get(d["ticker"]) or tidy(d["name"])
    keep.sort(key=lambda d: -d["cap"])                     # biggest companies first
    print("after the price rule:", len(keep))
    return keep, str(last_day.date())


def friday_message(dips, as_of, cfg):
    n = len(dips)
    title = f"Friday dips: {n} stock{'s' if n != 1 else ''} dropped suddenly"
    top = dips[:int(cfg["friday_list_max"])]
    lines = []
    for d in top:
        how = f"{d['wk_pct']:+.1f}% in 5 days" if d["wk_pct"] <= -float(cfg["sudden_drop_5day_pct"]) else f"{d['day_pct']:+.1f}% today"
        lines.append(f"{d['name']} ({d['ticker']})  ${d['price']:,.2f}  {how}")
    if n > len(top):
        lines.append(f"...and {n - len(top)} smaller companies.")
    lines.append("")
    lines.append(f"All US-listed stocks, prices of {as_of}. Biggest companies first.")
    return title, "\n".join(lines)


# -------------------------------------------------------------------- main ---
def main(argv):
    what = argv[0] if argv and not argv[0].startswith("-") else "auto"
    dry, force = "--dry" in argv, "--force" in argv
    cfg = load_cfg()
    state = load_json(SCANS_PATH, {})
    now_ny = dt.datetime.now(NY)
    today = now_ny.date().isoformat()
    if what == "auto":
        todo = ["analysts"] + (["friday"] if now_ny.weekday() == 4 and cfg.get("friday_scan_enabled") else [])
    else:
        todo = ["analysts", "friday"] if what == "both" else [what]

    import claude_news_core as cn
    app_cfg = cn.load_app_config()
    devices = None if dry else cn.load_devices()
    devices_changed = False

    def send(title, body):
        nonlocal devices_changed
        print("\n--- " + title + " ---\n" + body + "\n")
        if dry:
            return True
        res, ch = cn.notify(title, body, project="Stocks", cfg=app_cfg, state=devices)
        devices_changed |= ch
        return bool(res["feed"] or res["pushed"])

    if "analysts" in todo:
        run_analysts(cfg, state, argv, send, now_ny)

    if "friday" in todo:
        if state.get("friday", {}).get("date") == today and not force:
            print("friday check already done today")
        else:
            dips, as_of = friday_scan(cfg)
            ok = send(*friday_message(dips, as_of, cfg)) if dips else True
            if not dips:
                print("no sudden dips this week - nothing sent")
            if ok and not dry:
                state["friday"] = {"date": today, "as_of": as_of, "stocks": dips}

    if not dry:
        save_json(SCANS_PATH, state)
        if devices_changed:
            cn.save_devices(devices)


if __name__ == "__main__":
    main(sys.argv[1:])
