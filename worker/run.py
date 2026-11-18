"""
Stock checker that runs on GitHub (no PC needed).

Every run:
  1. reads the stock list and the picked stocks chosen in the Claude News app
     (data/settings.json, updated from the app through the "<topic>-settings" channel)
  2. downloads the price history of every stock on the list (2 years of daily prices,
     plus the last 5 trading days in 5-minute steps for the Today and 5-day charts) and
     writes data/stocks.json for the app's charts board (only when something changed)
  3. SUDDEN DIP alert for a picked stock that fell `sudden_drop_day_pct` in a day
     or `sudden_drop_5day_pct` in 5 trading days: says its all-time high and its
     30-day high and low (one alert per stock per `cooldown_hours`, unless it
     falls another `realert_extra_drop_pct`)
  4. WEEKLY message, Sunday 08:30 Israel time: the current price of the picked
     stocks and nothing else

State between runs lives in data/alert_state.json (committed by the workflow).
worker/loop.py calls this script: once per run outside trading hours, and on every quarter
hour while the US market is open and around the weekly message time.

    python worker/run.py            normal run
    python worker/run.py --dry      compute and write stocks.json only, send nothing
    python worker/run.py --weekly   also send the weekly message right now
    python worker/run.py --demo     also send the daily demo message right now
"""

import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "alert_state.json")
STOCKS_PATH = os.path.join(DATA_DIR, "stocks.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
sys.path.insert(0, HERE)

TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,12}$")
MAX_TICKERS = 30


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


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- settings ---
def load_settings(cfg):
    s = load_json(SETTINGS_PATH, {})
    if not isinstance(s.get("tickers"), dict) or not s["tickers"]:
        s["tickers"] = dict(cfg["tickers"])                 # first run: the default list
        s.setdefault("known", list(cfg["tickers"]))
    s.setdefault("picked", list(s["tickers"]))
    s.setdefault("known", [])
    s.setdefault("rejected", {})
    s.setdefault("name_tried", [])
    s.setdefault("since", None)
    s.setdefault("demo", False)                             # the app has a demo account: send the daily demo message
    s.setdefault("updated", None)
    return s


def clean_tickers(raw):
    out = []
    for t in raw:
        t = str(t).strip().upper()
        if TICKER_RE.match(t) and t not in out:
            out.append(t)
    return out[:MAX_TICKERS]


def sync_settings(app_cfg, cfg, settings=None):
    """Apply the stock list and picks chosen in the app. Returns (settings, changed)."""
    settings = settings or load_settings(cfg)
    before = json.dumps(settings, sort_keys=True)
    since = settings.get("since") or "all"
    url = f"{app_cfg['ntfy'].rstrip('/')}/{app_cfg['topic']}-settings/json?poll=1&since={since}"
    lines = []
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        lines = r.text.splitlines()
    except Exception as e:
        print("could not read app settings:", e)
    for line in lines:
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        if m.get("id"):
            settings["since"] = m["id"]
        try:
            data = json.loads(m.get("message", ""))
        except ValueError:
            continue
        if data.get("type") != "stocks":
            continue
        if isinstance(data.get("tickers"), list):
            wanted = clean_tickers(data["tickers"])
            if wanted != list(settings["tickers"]):
                print("stock list changed in the app:", wanted)
            names = data.get("names") if isinstance(data.get("names"), dict) else {}

            def name_for(t):
                if settings["tickers"].get(t, t) != t:
                    return settings["tickers"][t]                  # already known
                given = " ".join(str(names.get(t, "")).split())[:60]
                return given or t                                 # the app sends the name from its ticker list

            settings["tickers"] = {t: name_for(t) for t in wanted}
            for t in wanted:                                  # asked for again: give it another try
                settings["rejected"].pop(t, None)
        if isinstance(data.get("picked"), list):
            settings["picked"] = clean_tickers(data["picked"])
        if isinstance(data.get("demo"), bool):
            settings["demo"] = data["demo"]
        settings["updated"] = dt.datetime.fromtimestamp(m.get("time", 0), dt.timezone.utc).isoformat(timespec="seconds")
    settings["picked"] = [t for t in settings["tickers"] if t in settings["picked"]]
    return settings, json.dumps(settings, sort_keys=True) != before


# ------------------------------------------------------------- market data ---
def _table(data, field, symbols):
    """One price table (a column per stock) out of a Yahoo download."""
    import pandas as pd
    f = data[field]
    if isinstance(f, pd.Series):
        f = f.to_frame(name=symbols[0])
    return f


def fetch_daily(symbols):
    """Daily prices as far back as they go. Returns (closes, ohl): the closing prices, and the
    Open / High / Low tables that the app's candle charts are drawn from."""
    import yfinance as yf
    symbols = list(symbols)
    data = yf.download(symbols, period="max", interval="1d", auto_adjust=True, progress=False, threads=True)
    tables = {}
    for field in ("Close", "Open", "High", "Low"):
        try:
            f = _table(data, field, symbols)
        except KeyError:
            if field == "Close":
                raise
            continue
        if f.index.tz is not None:
            f.index = f.index.tz_localize(None)
        tables[field] = f
    return tables["Close"], {k: v for k, v in tables.items() if k != "Close"}


def fetch_closes(symbols):
    return fetch_daily(symbols)[0]


def candle_rows(closes, opens, highs, lows):
    """One [open, high, low] per closing price, in the same order. A missing value falls back
    on the close, and the high and low always enclose the open and the close."""
    rows = []
    for c, o, h, l in zip(closes, opens, highs, lows):
        c = float(c)
        o, h, l = (float(v) if v == v and v is not None else c for v in (o, h, l))    # NaN is not equal to itself
        rows.append([round(o, 2), round(max(o, h, c), 2), round(min(o, l, c), 2)])
    return rows


def fetch_intraday(symbols):
    """Prices of the last 5 trading days in 5-minute steps, for the app's Today and 5-day charts.

    Returns {ticker: {"today": [[minute, price], ...], "week": [...]}}, minutes since 1970 (UTC).
    "today" is the latest trading day in 5-minute steps; "week" is all 5 days in 15-minute steps.
    Returns {} when the download fails: the charts then keep what they had."""
    import pandas as pd
    import yfinance as yf
    symbols = list(symbols)
    try:
        data = yf.download(symbols, period="5d", interval="5m", auto_adjust=True, progress=False,
                           threads=True, prepost=False)
        closes = _table(data, "Close", symbols)
        idx = closes.index
        if idx.tz is None:
            idx = idx.tz_localize("America/New_York")
        closes.index = idx.tz_convert("UTC")
        ohl = {}
        for field in ("Open", "High", "Low"):                 # for the candle charts; fine without them
            try:
                f = _table(data, field, symbols)
                f.index = closes.index
                ohl[field] = f
            except Exception:
                ohl = {}
                break
        out = {}
        for t in closes.columns:
            s = closes[t].dropna()
            if len(s) < 2:
                continue
            days = list(s.index.tz_convert("America/New_York").strftime("%Y-%m-%d"))
            pts = [[int(ts.value // 60_000_000_000), round(float(v), 2)] for ts, v in s.items()]
            today = [p for p, d in zip(pts, days) if d == days[-1]]
            week = [p for p in pts if p[0] % 15 == 0]
            if not week or week[-1] != pts[-1]:
                week.append(pts[-1])                      # always end on the newest price
            out[str(t)] = {"today": today, "week": week}
            if ohl:
                try:
                    bars = candle_rows(s.values, *(ohl[f][t].reindex(s.index).values for f in ("Open", "High", "Low")))
                    out[str(t)]["today_c"] = bars[len(bars) - len(today):]
                    # one candle per 15-minute point: the 5-minute bars since the point before it, same day only
                    at = {p[0]: i for i, p in enumerate(pts)}
                    wk, before = [], -1
                    for p in week:
                        i = at[p[0]]
                        first = before + 1
                        while first < i and days[first] != days[i]:
                            first += 1
                        grp = bars[first:i + 1]
                        wk.append([grp[0][0], max(g[1] for g in grp), min(g[2] for g in grp)])
                        before = i
                    out[str(t)]["week_c"] = wk
                except Exception as e:
                    print("no intraday candles for", t, type(e).__name__)
        return out
    except Exception as e:
        print("no intraday prices this time:", type(e).__name__, e)
        return {}


def add_intraday(results, old_board):
    """Attach the Today and 5-day chart points to every stock. If Yahoo gives none this time,
    keep the points the last check saved."""
    fresh = fetch_intraday([r["ticker"] for r in results]) if results else {}
    old = {s.get("ticker"): s for s in (old_board or {}).get("stocks", []) if isinstance(s, dict)}
    for r in results:
        src = fresh.get(r["ticker"]) or old.get(r["ticker"]) or {}
        for key in ("today", "week", "today_c", "week_c"):
            if src.get(key):
                r[key] = src[key]


def lookup_name(symbol):
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        name = info.get("shortName") or info.get("longName") or symbol
        # "NVIDIA Corporation" / "Advanced Micro Devices, Inc." read better without the legal tail
        return re.sub(r",?\s+(Inc\.?|Corporation|Corp\.?|Ltd\.?|PLC|plc|N\.V\.|S\.A\.|Holdings?|Company|Co\.)\s*$", "", name).strip() or symbol
    except Exception:
        return symbol


def slice_years(s, years):
    import pandas as pd
    if years is None or len(s) == 0:
        return s
    return s[s.index >= s.index[-1] - pd.Timedelta(days=int(years * 365.25))]


def analyze(ticker, name, series, cfg, ohl=None):
    s = series.dropna()
    if len(s) < 7:
        return None
    lookback = int(cfg["lookback_days"])
    years = int(cfg["longterm_years"])
    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
    day_pct = (last / prev - 1) * 100
    chg5_pct = (last / float(s.iloc[-6]) - 1) * 100

    window = s.iloc[-lookback:]
    high, low30 = float(window.max()), float(window.min())
    from_high_pct = (last / high - 1) * 100

    long = slice_years(s, years)
    lt_high = float(long.max())
    lt_high_date = str(long.idxmax().date())
    from_lt_high_pct = (last / lt_high - 1) * 100

    year = slice_years(s, 1)
    yr_low = float(year.min())

    # the sudden-dip rule (this is what sends an alert)
    sudden_reasons = []
    if day_pct <= -float(cfg["sudden_drop_day_pct"]):
        sudden_reasons.append(f"down {abs(day_pct):.1f}% today")
    if chg5_pct <= -float(cfg["sudden_drop_5day_pct"]):
        sudden_reasons.append(f"down {abs(chg5_pct):.1f}% in 5 trading days")

    # slower "how far below its highs" tags, shown on the board only
    reasons = []
    big = from_lt_high_pct <= -float(cfg["drop_from_longterm_high_pct"])
    if big:
        reasons.append(f"{abs(from_lt_high_pct):.0f}% below its {years}-year high (${lt_high:,.2f} on {lt_high_date})")
    if from_high_pct <= -float(cfg["drop_from_high_pct"]):
        reasons.append(f"{abs(from_high_pct):.1f}% below its {lookback}-day high (${high:,.2f})")
    if day_pct <= -float(cfg["daily_drop_pct"]):
        reasons.append(f"down {abs(day_pct):.1f}% today")

    out = {
        "ticker": ticker, "name": name,
        "price": round(last, 2), "prev": round(prev, 2), "day_pct": round(day_pct, 2),
        "chg5_pct": round(chg5_pct, 2),
        "ath": round(float(s.max()), 2), "ath_date": str(s.idxmax().date()),
        "atl": round(float(s.min()), 2), "atl_date": str(s.idxmin().date()),
        "yr_low": round(yr_low, 2), "yr_low_date": str(year.idxmin().date()),
        "from_yr_low_pct": round((last / yr_low - 1) * 100, 2),
        "high30": round(high, 2), "high30_date": str(window.idxmax().date()),
        "low30": round(low30, 2), "low30_date": str(window.idxmin().date()),
        "from_high_pct": round(from_high_pct, 2),
        "lt_high": round(lt_high, 2), "lt_high_date": lt_high_date,
        "from_lt_high_pct": round(from_lt_high_pct, 2),
        "sudden": bool(sudden_reasons), "sudden_reasons": sudden_reasons,
        "dip": bool(reasons), "big_dip": big, "reasons": reasons,
        "as_of": str(s.index[-1].date()),
        # 2 years of daily closes for the charts: [days since 1970, close]
        "series": [[int(ts.value // 86_400_000_000_000), round(float(v), 2)] for ts, v in long.items()],
    }
    if ohl:                                                   # [open, high, low] for every point of "series"
        try:
            out["candles"] = candle_rows(long.values, *(ohl[f][ticker].reindex(long.index).values for f in ("Open", "High", "Low")))
        except Exception as e:
            print("no candles for", ticker, type(e).__name__)
    return out


def build_results(settings, cfg, closes, ohl=None):
    """Analyze every stock on the list. Stocks added in the app that the market does
    not know are moved to settings['rejected']. Returns (results, settings changed)."""
    changed = False
    results = []
    for ticker in list(settings["tickers"]):
        r = None
        if ticker in closes.columns:
            r = analyze(ticker, settings["tickers"][ticker], closes[ticker], cfg, ohl)
        if r:
            results.append(r)
    got = {r["ticker"] for r in results}
    for ticker in list(settings["tickers"]):
        if ticker in got:
            if ticker not in settings["known"]:
                settings["known"].append(ticker)
                changed = True
            if settings["tickers"][ticker] == ticker and ticker not in settings["name_tried"]:
                settings["name_tried"].append(ticker)
                name = lookup_name(ticker)
                settings["tickers"][ticker] = name
                for r in results:
                    if r["ticker"] == ticker:
                        r["name"] = name
                print("new stock:", ticker, "=", name)
                changed = True
        elif ticker not in settings["known"] and got:
            # never had data, while other stocks do: not a real ticker
            print("no such stock:", ticker)
            del settings["tickers"][ticker]
            settings["rejected"][ticker] = utc_now()
            changed = True
        else:
            print("no data this time for", ticker)
    if changed:
        settings["picked"] = [t for t in settings["tickers"] if t in settings["picked"]]
        for old in sorted(settings["rejected"], key=settings["rejected"].get)[:-20]:
            del settings["rejected"][old]
    return results, changed


# ------------------------------------------------------------------ alerts ---
def sudden_alert_due(r, state, cfg):
    if not r["sudden"]:
        return False
    prev = state.get("sudden", {}).get(r["ticker"])
    if not prev:
        return True
    hours = (dt.datetime.now() - dt.datetime.fromisoformat(prev["time"])).total_seconds() / 3600
    if hours >= float(cfg["cooldown_hours"]):
        return True
    return (r["price"] / prev["price"] - 1) * 100 <= -float(cfg["realert_extra_drop_pct"])


def sudden_message(r):
    title = f"Sudden dip: {r['name']} ({r['ticker']})"
    body = "\n".join([
        "; ".join(r["sudden_reasons"]).capitalize() + ".",
        f"Now ${r['price']:,.2f}",
        f"All-time high ${r['ath']:,.2f} ({r['ath_date']})",
        f"30-day high ${r['high30']:,.2f}",
        f"30-day low ${r['low30']:,.2f}",
    ])
    return title, body


def weekly_due(state, cfg, now, spec=None, key="_weekly"):
    """True once per week, from `time` on `weekday` (Monday is 0, Sunday is 6)."""
    w = spec or cfg["weekly"]
    hh, mm = (int(x) for x in w["time"].split(":"))
    if now.weekday() != int(w["weekday"]):
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    return state.get(key, {}).get("date") != now.date().isoformat()


def daily_due(state, spec, now, key="_demo"):
    """True once per day, from `time` until `late_minutes` after it (a late message is no use)."""
    hh, mm = (int(x) for x in spec["time"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not target <= now <= target + dt.timedelta(minutes=int(spec.get("late_minutes", 120))):
        return False
    return state.get(key, {}).get("date") != now.date().isoformat()


def demo_message(results, now):
    """The daily demo message. The demo account itself lives on the phone, so this only invites
    to open it; the link makes a tap open the demo screen."""
    ny_today = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    traded = any(r.get("as_of") == ny_today for r in results)
    body = ("See how your demo stocks are doing today." if traded
            else "The US market is closed today, so prices have not moved since the last trading day.")
    return "Your demo account", body + "\nTap to open it."


def demo_url(cfg):
    base = str(cfg.get("app_url") or "").strip()
    return (base.rstrip("/") + "/?tab=Stocks&open=demo") if base else None


INSTAGRAM_REMINDER = ("Weekly Instagram check",
                      "It's time to see who doesn't follow you back.\n"
                      "1. In Instagram, request your export: Followers and following, format JSON.\n"
                      "2. When the file is ready, open the Instagram tab here, press Check and load it.\n"
                      "The steps are listed inside Check.")


def weekly_message(results, now):
    title = f"Your stocks - {now:%A %d %b}"
    body = "\n".join(f"{r['name']} ({r['ticker']})  ${r['price']:,.2f}" for r in results)
    return title, body


def same_board(old, new, cents=0.011):
    """True when the charts data did not really change.

    Yahoo re-serves old closing prices with one-cent rounding wobbles, which made every run
    look like a change. Everything except the history must match exactly; a history point may
    differ by a cent. A real move always changes the current price, which is compared exactly."""
    def split(board):
        board = json.loads(json.dumps(board))
        series = [s.pop(key, []) for s in board.get("stocks", []) if isinstance(s, dict)
                  for key in ("series", "today", "week", "candles", "today_c", "week_c")]
        return board, series
    try:
        a, sa = split(old)
        b, sb = split(new)
        if a != b or len(sa) != len(sb):
            return False
        for x, y in zip(sa, sb):
            if len(x) != len(y):
                return False
            for row1, row2 in zip(x, y):                     # [day, price] or [open, high, low]
                if len(row1) != len(row2) or any(abs(a - b) > cents for a, b in zip(row1, row2)):
                    return False
        return True
    except (TypeError, ValueError, AttributeError):
        return False


# -------------------------------------------------------------------- main ---
def main(argv):
    dry = "--dry" in argv
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("worker/config.json missing")
    state = load_json(STATE_PATH, {})

    import claude_news_core as cn
    app_cfg = cn.load_app_config()
    settings, settings_changed = sync_settings(app_cfg, cfg)

    closes, ohl = fetch_daily(settings["tickers"]) if settings["tickers"] else (None, None)
    results, ch = build_results(settings, cfg, closes, ohl) if closes is not None else ([], False)
    add_intraday(results, load_json(STOCKS_PATH, {}))
    settings_changed |= ch
    picked = settings["picked"]
    print("list:", ", ".join(settings["tickers"]) or "(empty)")
    print("picked:", ", ".join(picked) or "(none)")
    for r in results:
        print(f"{r['ticker']:<6} ${r['price']:>9,.2f}  day {r['day_pct']:+6.2f}%  5d {r['chg5_pct']:+6.2f}%  "
              f"{'SUDDEN DIP' if r['sudden'] else ''}{'' if r['ticker'] in picked else '  (not picked)'}")

    if settings_changed or not os.path.exists(SETTINGS_PATH):
        save_json(SETTINGS_PATH, settings)

    import tickers
    tickers.refresh_if_stale()            # the searchable list the app uses when adding a stock

    board = {
        "picked": picked,
        "rejected": settings["rejected"],
        "rules": {k: cfg[k] for k in ("longterm_years", "drop_from_longterm_high_pct", "lookback_days",
                                      "drop_from_high_pct", "daily_drop_pct",
                                      "sudden_drop_day_pct", "sudden_drop_5day_pct")},
        "weekly": cfg["weekly"],
        "stocks": results,
    }
    old = load_json(STOCKS_PATH, {})
    old.pop("updated", None)
    if results and not same_board(old, board):               # market closed: nothing moves, nothing to commit
        save_json(STOCKS_PATH, {"updated": utc_now(), **board}, compact=True)
        print("wrote", STOCKS_PATH)
    else:
        print("stocks.json unchanged")
    if dry or not results:
        return

    devices = cn.load_devices()
    devices_changed = False
    mine = [r for r in results if r["ticker"] in picked]

    for r in mine:
        if sudden_alert_due(r, state, cfg):
            title, body = sudden_message(r)
            res, ch = cn.notify(title, body, project="Stocks", priority="high", cfg=app_cfg, state=devices)
            devices_changed |= ch
            if res["feed"] or res["pushed"]:
                state.setdefault("sudden", {})[r["ticker"]] = {
                    "time": dt.datetime.now().isoformat(timespec="seconds"), "price": r["price"]}

    now = dt.datetime.now(ZoneInfo(cfg["weekly"].get("timezone", "Asia/Jerusalem")))
    if mine and ("--weekly" in argv or weekly_due(state, cfg, now)):
        title, body = weekly_message(mine, now)
        res, ch = cn.notify(title, body, project="Stocks", cfg=app_cfg, state=devices)
        devices_changed |= ch
        if res["feed"] or res["pushed"]:
            state["_weekly"] = {"date": now.date().isoformat()}

    # The daily demo message, only while the app says a demo account exists.
    dd = cfg.get("demo_daily")
    if dd and (settings.get("demo") or "--demo" in argv):
        now_d = dt.datetime.now(ZoneInfo(dd.get("timezone", "Asia/Jerusalem")))
        if "--demo" in argv or daily_due(state, dd, now_d):
            title, body = demo_message(results, now_d)
            res, ch = cn.notify(title, body, project="Stocks", url=demo_url(cfg), cfg=app_cfg, state=devices)
            devices_changed |= ch
            if res["feed"] or res["pushed"]:
                state["_demo"] = {"date": now_d.date().isoformat()}

    # Instagram: the follow-back check needs a fresh export from the user, so this is a reminder.
    # (Instagram offers no approved way for a program to read follower lists by itself.)
    ig = cfg.get("instagram_reminder")
    if ig:
        now_ig = dt.datetime.now(ZoneInfo(ig.get("timezone", "Asia/Jerusalem")))
        if "--instagram" in argv or weekly_due(state, cfg, now_ig, spec=ig, key="_instagram"):
            res, ch = cn.notify(*INSTAGRAM_REMINDER, project="Instagram", cfg=app_cfg, state=devices)
            devices_changed |= ch
            if res["feed"] or res["pushed"]:
                state["_instagram"] = {"date": now_ig.date().isoformat()}

    # pick up newly registered phones so the next message reaches them
    devices_changed |= cn.refresh_devices(app_cfg, devices)

    if state != load_json(STATE_PATH, {}):
        save_json(STATE_PATH, state)
    if devices_changed:
        cn.save_devices(devices)


if __name__ == "__main__":
    main(sys.argv[1:])
