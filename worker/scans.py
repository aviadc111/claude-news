"""
Market-wide scans that run on GitHub after the US market closes.

  analysts   every trading day: which large companies did many analysts rate "Buy" today?
             If any stock got `analyst_min_buy_ratings` or more Buy-type ratings from
             different firms, send its name, price, all-time high and 30-day high / low.
             These are analysts' published ratings, reported as they are. Not advice.

  friday     every Friday: every US-listed stock (not only the picked ones) that had a
             sudden dip - down `sudden_drop_day_pct` today or `sudden_drop_5day_pct` in
             5 trading days - and that either traded at `friday_recent_high` dollars or
             more in the last 3 months, or once cost `friday_alltime_high` dollars or more.

    python worker/scans.py auto               what the schedule runs (analysts; plus friday on Fridays)
    python worker/scans.py analysts|friday|both [--dry] [--force] [--hours N]
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
    "analyst_max_stocks": 3,
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
        print(f"  prices {min(i + batch, len(symbols))}/{len(symbols)}", flush=True)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out.loc[:, ~out.columns.duplicated()]


# ---------------------------------------------------------- analysts (daily) ---
def analyst_scan(cfg, hours):
    import pandas as pd
    import yfinance as yf
    Q = yf.EquityQuery
    quotes = [q for q in universe(Q("gt", ["intradaymarketcap", int(cfg["analyst_min_market_cap"])]))
              if q.get("averageAnalystRating")]
    print("companies with analyst coverage:", len(quotes))
    since = pd.Timestamp(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).tz_localize(None)

    def fetch(q):
        sym = q["symbol"]
        try:
            df = yf.Ticker(sym).upgrades_downgrades
            if df is None or df.empty:
                return None
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df = df[df.index >= since]
            if df.empty:
                return None
            buy, other, upgrades, raised, lowered = set(), set(), set(), set(), set()
            for firm, row in df[~df["Firm"].duplicated()].set_index("Firm").iterrows():   # newest action per firm
                grade = str(row["ToGrade"]).strip().lower()
                if grade in BUY_GRADES:
                    buy.add(firm)
                    if str(row["Action"]).lower() in ("up", "init"):
                        upgrades.add(firm)
                    target = str(row.get("priceTargetAction", "")).lower()
                    if target.startswith("raise"):
                        raised.add(firm)
                    elif target.startswith("lower"):
                        lowered.add(firm)
                elif grade:
                    other.add(firm)
            if not buy:
                return None
            return {"ticker": sym, "buy": len(buy), "upgrades": len(upgrades), "other": len(other),
                    "raised": len(raised), "lowered": len(lowered),
                    "firms": sorted(buy)[:12], "name": q.get("shortName") or sym}
        except Exception:
            return "error"

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(fetch, quotes))
    errors = sum(1 for r in res if r == "error")
    hits = sorted((r for r in res if isinstance(r, dict) and r["buy"] >= int(cfg["analyst_min_buy_ratings"])),
                  key=lambda r: (-r["buy"], -r["upgrades"], r["other"]))
    print(f"lookups failed: {errors} | stocks with {cfg['analyst_min_buy_ratings']}+ buy ratings: "
          f"{[(r['ticker'], r['buy']) for r in hits]}")
    hits = hits[:int(cfg["analyst_max_stocks"])]
    if hits:
        names = nice_names()
        full = closes([r["ticker"] for r in hits], "max")
        for r in hits:
            r["name"] = names.get(r["ticker"]) or tidy(r["name"])
            s = full[r["ticker"]].dropna() if r["ticker"] in full.columns else None
            if s is not None and len(s) > 1:
                w = s.iloc[-30:]
                r.update(price=round(float(s.iloc[-1]), 2), ath=round(float(s.max()), 2), ath_date=str(s.idxmax().date()),
                         high30=round(float(w.max()), 2), low30=round(float(w.min()), 2))
    return hits


def analyst_message(hits):
    title = "Analyst buy ratings today: " + ", ".join(r["ticker"] for r in hits)
    parts = []
    for r in hits:
        up = f", {r['upgrades']} of them new or upgraded" if r["upgrades"] else ""
        neg = f" {r['other']} rated it Hold or lower." if r["other"] else ""
        lines = [f"{r['name']} ({r['ticker']}): {r['buy']} analyst firms rated it Buy today{up}.{neg}"]
        if r.get("raised") or r.get("lowered"):
            lines.append(f"Their price targets: {r.get('raised', 0)} raised, {r.get('lowered', 0)} lowered.")
        if "price" in r:
            lines += [f"Now ${r['price']:,.2f}", f"All-time high ${r['ath']:,.2f} ({r['ath_date']})",
                      f"30-day high ${r['high30']:,.2f}", f"30-day low ${r['low30']:,.2f}"]
        parts.append("\n".join(lines))
    return title, "\n\n".join(parts) + "\n\nThese are analysts' published ratings, not advice."


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
        if now_ny.weekday() > 4:
            print("weekend - nothing to scan")
            return
        todo = ["analysts"] + (["friday"] if now_ny.weekday() == 4 else [])
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
        if state.get("analysts", {}).get("date") == today and not force:
            print("analyst check already done today")
        else:
            hours = int(argv[argv.index("--hours") + 1]) if "--hours" in argv else (72 if now_ny.weekday() == 0 else 24)
            hits = analyst_scan(cfg, hours)
            ok = send(*analyst_message(hits)) if hits else True
            if not hits:
                print("no stock got a lot of buy ratings today - nothing sent")
            if ok and not dry:
                state["analysts"] = {"date": today, "stocks": hits}

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
