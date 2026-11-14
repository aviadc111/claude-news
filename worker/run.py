"""
Stock dip checker that runs on GitHub (no PC needed).

Every run:
  1. downloads the full price history of the stocks in worker/config.json
  2. writes data/stocks.json for the Claude News app's stocks board
  3. sends a dip alert to the phone when a stock crosses a rule (with cooldown)
  4. sends the all-stocks summary when the last one is older than summary_every_hours

State between runs lives in data/alert_state.json (committed by the workflow).

    python worker/run.py            normal run
    python worker/run.py --dry      compute and write stocks.json only, send nothing
"""

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "alert_state.json")
STOCKS_PATH = os.path.join(DATA_DIR, "stocks.json")
sys.path.insert(0, HERE)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, separators=(",", ":"))
        else:
            json.dump(data, f, indent=1)


# ------------------------------------------------------------- market data ---
def fetch_closes(symbols):
    import pandas as pd
    import yfinance as yf
    symbols = list(symbols)
    data = yf.download(symbols, period="max", interval="1d", auto_adjust=True, progress=False, threads=True)
    closes = data["Close"]
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(name=symbols[0])
    if closes.index.tz is not None:
        closes.index = closes.index.tz_localize(None)
    return closes


def slice_years(s, years):
    import pandas as pd
    if years is None or len(s) == 0:
        return s
    return s[s.index >= s.index[-1] - pd.Timedelta(days=int(years * 365.25))]


def analyze(ticker, name, series, cfg):
    s = series.dropna()
    if len(s) < 2:
        return None
    lookback = int(cfg["lookback_days"])
    years = int(cfg["longterm_years"])
    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
    day_pct = (last / prev - 1) * 100

    window = s.iloc[-lookback:]
    high, low30 = float(window.max()), float(window.min())
    from_high_pct = (last / high - 1) * 100

    long = slice_years(s, years)
    lt_high = float(long.max())
    lt_high_date = str(long.idxmax().date())
    from_lt_high_pct = (last / lt_high - 1) * 100

    year = slice_years(s, 1)
    yr_low = float(year.min())

    reasons = []
    big = from_lt_high_pct <= -float(cfg["drop_from_longterm_high_pct"])
    if big:
        reasons.append(f"{abs(from_lt_high_pct):.0f}% below its {years}-year high (${lt_high:,.2f} on {lt_high_date})")
    if from_high_pct <= -float(cfg["drop_from_high_pct"]):
        reasons.append(f"{abs(from_high_pct):.1f}% below its {lookback}-day high (${high:,.2f})")
    if day_pct <= -float(cfg["daily_drop_pct"]):
        reasons.append(f"down {abs(day_pct):.1f}% today")

    return {
        "ticker": ticker, "name": name,
        "price": round(last, 2), "prev": round(prev, 2), "day_pct": round(day_pct, 2),
        "ath": round(float(s.max()), 2), "ath_date": str(s.idxmax().date()),
        "atl": round(float(s.min()), 2), "atl_date": str(s.idxmin().date()),
        "yr_low": round(yr_low, 2), "yr_low_date": str(year.idxmin().date()),
        "from_yr_low_pct": round((last / yr_low - 1) * 100, 2),
        "high30": round(high, 2), "high30_date": str(window.idxmax().date()),
        "low30": round(low30, 2), "low30_date": str(window.idxmin().date()),
        "from_high_pct": round(from_high_pct, 2),
        "lt_high": round(lt_high, 2), "lt_high_date": lt_high_date,
        "from_lt_high_pct": round(from_lt_high_pct, 2),
        "dip": bool(reasons), "big_dip": big, "reasons": reasons,
        "as_of": str(s.index[-1].date()),
        # 2 years of daily closes for the charts: [days since 1970, close]
        "series": [[int(ts.value // 86_400_000_000_000), round(float(v), 2)] for ts, v in long.items()],
    }


# ------------------------------------------------------------------ alerts ---
def should_alert(r, state, cfg):
    if not r["dip"]:
        return False
    prev = state.get(r["ticker"])
    if not prev:
        return True
    hours = (dt.datetime.now() - dt.datetime.fromisoformat(prev["time"])).total_seconds() / 3600
    if hours >= float(cfg["cooldown_hours"]):
        return True
    return (r["price"] / prev["price"] - 1) * 100 <= -float(cfg["realert_extra_drop_pct"])


def summary_due(state, cfg):
    prev = state.get("_summary")
    if not prev:
        return True
    hours = (dt.datetime.now() - dt.datetime.fromisoformat(prev["time"])).total_seconds() / 3600
    return hours >= float(cfg["summary_every_hours"]) - 0.1   # a few minutes of cron drift is fine


def build_summary(results):
    big = [r["name"] for r in results if r["big_dip"]]
    dip = [r["name"] for r in results if r["dip"] and not r["big_dip"]]
    normal = [r["name"] for r in results if not r["dip"]]
    lines = ["BIG DIP: " + (", ".join(big) or "none"), "DIP: " + (", ".join(dip) or "none"),
             "Normal: " + (", ".join(normal) or "none"), ""]
    for r in sorted(results, key=lambda r: r["from_lt_high_pct"]):
        tag = "  BIG DIP" if r["big_dip"] else ("  DIP" if r["dip"] else "")
        lines.append(f"{r['name']} ({r['ticker']})  ${r['price']:,.2f}  {r['day_pct']:+.1f}% today{tag}")
        lines.append(f"   all-time high ${r['ath']:,.2f} · biggest dip ${r['yr_low']:,.2f} · "
                     f"30d high ${r['high30']:,.2f} · 30d low ${r['low30']:,.2f}")
    n = len(big)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=2)))   # Israel time for the title
    return f"Stocks {now:%H:%M} - {n} big dip{'s' if n != 1 else ''}", "\n".join(lines)


# -------------------------------------------------------------------- main ---
def main(argv):
    dry = "--dry" in argv
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("worker/config.json missing")
    state = load_json(STATE_PATH, {})

    closes = fetch_closes(cfg["tickers"])
    results = []
    for ticker, name in cfg["tickers"].items():
        if ticker not in closes.columns:
            print("no data for", ticker)
            continue
        r = analyze(ticker, name, closes[ticker], cfg)
        if r:
            results.append(r)
            print(f"{ticker:<5} ${r['price']:>9,.2f}  day {r['day_pct']:+6.2f}%  vs {cfg['longterm_years']}y high "
                  f"{r['from_lt_high_pct']:+6.2f}%  {'BIG DIP' if r['big_dip'] else ('DIP' if r['dip'] else '')}")

    save_json(STOCKS_PATH, {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rules": {k: cfg[k] for k in ("longterm_years", "drop_from_longterm_high_pct", "lookback_days",
                                      "drop_from_high_pct", "daily_drop_pct")},
        "stocks": results,
    }, compact=True)
    print("wrote", STOCKS_PATH)
    if dry or not results:
        return

    import claude_news_core as cn
    app_cfg = cn.load_app_config()
    devices = cn.load_devices()
    devices_changed = False

    for r in results:
        if should_alert(r, state, cfg):
            kind = "BIG DIP" if r["big_dip"] else "dip"
            title = f"{kind}: {r['name']} ({r['ticker']}) at ${r['price']:,.2f}"
            body = "; ".join(r["reasons"]).capitalize() + ".\nWorth a look."
            res, ch = cn.notify(title, body, project="Stocks", priority="high", cfg=app_cfg, state=devices)
            devices_changed |= ch
            if res["feed"] or res["pushed"]:
                state[r["ticker"]] = {"time": dt.datetime.now().isoformat(timespec="seconds"), "price": r["price"]}

    if summary_due(state, cfg):
        title, body = build_summary(results)
        res, ch = cn.notify(title, body, project="Stocks", cfg=app_cfg, state=devices)
        devices_changed |= ch
        if res["feed"] or res["pushed"]:
            state["_summary"] = {"time": dt.datetime.now().isoformat(timespec="seconds")}
    else:
        # still pick up newly registered phones so the next alert reaches them
        devices_changed |= cn.refresh_devices(app_cfg, devices)

    save_json(STATE_PATH, state)
    if devices_changed:
        cn.save_devices(devices)


if __name__ == "__main__":
    main(sys.argv[1:])
