"""
A message when one of the demo's lines is reached.

In the app's candles window the user draws lines at a price: "buy when it rises to X", "sell when
it drops to Y". The demo account lives on the phone, so the buying and selling happens in the app
(linesSync in index.html) the next time it is open. For a message at the moment itself, the app
tells the checker where the lines are, and nothing more about the account:

    {"type": "lines", "lines": [{"id", "t", "side", "dir", "price", "until"}], "own": [tickers], "cash": bool}

    side   "buy" or "sell"            dir    "above" (the price has to rise to it) or "below"
    until  milliseconds: the price has been followed up to here
    own    the stocks with a Sell line that the demo holds (a Sell line waits while it holds none)
    cash   whether the demo has money to buy with

No amounts and no balances. The lines are kept encrypted in data/lines.enc (same key as the phone
list), so nothing readable is published.

check() follows the price in the same 15-minute steps, with the same rules and in the same order
as the app, so the message and the app's trade agree: a line is reached when a step's high (for
"above") or low (for "below") touches it, and it fills at the line's price, or at the step's
opening price when the price jumped past the line.
"""

import json
import os
import re

import claude_news_core as cn

LINES_ENC = os.path.join(cn.DATA_DIR, "lines.enc")
MAX_LINES = 20
ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,12}$")


def empty():
    return {"lines": [], "own": [], "cash": True, "fired": []}


def load():
    if not os.path.exists(LINES_ENC):
        return empty()
    with open(LINES_ENC, "rb") as f:
        state = json.loads(cn._fernet().decrypt(f.read()).decode("utf-8"))
    return {**empty(), **state}


def save(state):
    os.makedirs(cn.DATA_DIR, exist_ok=True)
    with open(LINES_ENC, "wb") as f:
        f.write(cn._fernet().encrypt(json.dumps(state).encode("utf-8")))


def clean(payload):
    """The lines of a message from the app, checked: the channel is not a trusted place."""
    out = []
    for L in payload.get("lines") if isinstance(payload.get("lines"), list) else []:
        try:
            line = {"id": str(L["id"]), "t": str(L["t"]).upper(), "side": L["side"], "dir": L["dir"],
                    "price": round(float(L["price"]), 4), "until": int(L["until"])}
        except (KeyError, TypeError, ValueError):
            continue
        if (ID_RE.match(line["id"]) and TICKER_RE.match(line["t"]) and line["side"] in ("buy", "sell")
                and line["dir"] in ("above", "below") and 0 < line["price"] < 10_000_000 and line["until"] > 0
                and all(x["id"] != line["id"] for x in out)):
            out.append(line)
    return out[:MAX_LINES]


def merge(state, payload):
    """Take the app's newest list. Returns True when the saved state changed."""
    before = json.dumps(state, sort_keys=True)
    old = {L["id"]: L for L in state["lines"]}
    lines = []
    for L in clean(payload):
        if L["id"] in state["fired"]:
            continue                                         # already reached here; the app has not caught up yet
        if L["id"] in old:
            L["until"] = max(L["until"], old[L["id"]]["until"])
        lines.append(L)
    state["lines"] = lines
    own = payload.get("own") if isinstance(payload.get("own"), list) else []
    state["own"] = sorted({str(t).upper() for t in own if TICKER_RE.match(str(t).upper())})[:MAX_LINES]
    state["cash"] = payload.get("cash") is not False
    return json.dumps(state, sort_keys=True) != before


def steps(r):
    """A stock's 15-minute steps of the last five trading days: (time in ms, open, high, low)."""
    week, bars = r.get("week") or [], r.get("week_c") or []
    if not week or len(week) != len(bars):
        return []
    return [(p[0] * 60000, b[0], b[1], b[2]) for p, b in zip(week, bars)]


def check(state, results):
    """Carry the lines forward over the newest prices. Returns the lines that were reached now:
    [{"line", "name", "price", "time", "cash"}]. The state is changed in place."""
    by = {r["ticker"]: r for r in results}
    events = []
    for L in state["lines"]:
        r = by.get(L["t"])
        if not r:
            continue                                         # off the list for now: the line waits
        for when, o, h, l in steps(r):
            if when > L["until"]:
                events.append((when, 0 if L["side"] == "sell" else 1, L, o, h, l))
    events.sort(key=lambda e: (e[0], e[1]))                  # in order of time; at the same moment a sale goes first
    own, done, fired = set(state["own"]), set(), []
    for when, _, L, o, h, l in events:
        if L["id"] in done:
            continue
        L["until"] = when
        above = L["dir"] == "above"
        if not (h >= L["price"] if above else l <= L["price"]):
            continue
        price = max(o, L["price"]) if above else min(o, L["price"])
        cash = state["cash"]
        if L["side"] == "sell":
            if L["t"] not in own:
                continue                                     # nothing to sell yet: keep waiting
            own.discard(L["t"])
            state["cash"] = True                             # a sale brings money in
        elif cash:
            own.add(L["t"])
        done.add(L["id"])
        fired.append({"line": dict(L), "name": by[L["t"]].get("name") or L["t"], "price": price, "time": when, "cash": cash})
    if done:
        state["lines"] = [L for L in state["lines"] if L["id"] not in done]
        state["own"] = sorted(own)
        state["fired"] = (state["fired"] + sorted(done))[-200:]
    return fired


def message(f):
    """(title, body) for one reached line. No amounts: the message shows on the lock screen."""
    L, name = f["line"], f["name"]
    way = "rose" if L["dir"] == "above" else "fell"
    if abs(f["price"] - L["price"]) >= 0.005:
        where = f"{way} past your line at ${L['price']:,.2f} and opened at ${f['price']:,.2f}"
    else:
        where = f"{way} to your line at ${L['price']:,.2f}"
    if L["side"] == "sell":
        title, did = f"Your line sold {name}", "Your demo sold its shares there."
    elif f["cash"]:
        title, did = f"Your line bought {name}", "Your demo bought it there."
    else:
        title, did = f"Your Buy line on {name} was reached", "Your demo had no cash, so nothing was bought."
    return title, f"{name} ({L['t']}) {where}. {did}\nTap to open your demo."
