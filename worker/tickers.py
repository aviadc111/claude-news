"""
Builds data/tickers.txt: every US-listed stock and fund (NASDAQ, NYSE, NYSE American,
NYSE Arca, Cboe), one per line as  SYMBOL|Name , from the official NASDAQ Trader
symbol directory. The Claude News app searches this list while you type a stock to
add, and refuses tickers that are not in it.

    python worker/tickers.py          rebuild now
The checker (run.py) rebuilds it when it is missing or older than 7 days.
"""

import datetime as dt
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "data", "tickers.txt")
SOURCES = {
    "nasdaq": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "other": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}
TICKER_RE = re.compile(r"^[A-Z0-9\-]{1,10}$")
# warrants, SPAC units, rights, preferreds, notes: not what "add a stock" means
SKIP_NAME = re.compile(r"Warrant|\bRights?\b|Preferred|Depositary Shares?,? [Ee]ach [Rr]epresenting a|\bNotes?\b|Debenture|"
                       r"\d\s?%|- Units?$|\bUnits?,? each|Units? consisting|Subordinated|Contingent Value", re.I)
TAIL = re.compile(r"\s*(-\s*)?("
                  r"American Deposit[ao]ry Shares?.*|American Deposit[ao]ry Receipts?.*|Deposit[ao]ry Shares?.*|ADS\b.*|"
                  r"Common Stock.*|Common Shares?.*|Ordinary Shares?.*|Capital Stock.*|"
                  r"Shares of Beneficial Interest.*|Beneficial Interest.*|Common Units.*|Registered Shares?.*|"
                  r"Subordinate Voting Shares?.*|New York Registry Shares?.*)$", re.I)
LEGAL = re.compile(r",?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Ltd\.?|Limited|PLC|plc|N\.V\.|S\.A\.|SE|AG|"
                   r"Co\.|Company|L\.P\.|LP|LLC|Holdings?|Group)$")


def clean_name(name):
    name = " ".join(name.split())
    cls = re.search(r"\bClass ([A-Z])\b", name)
    name = TAIL.sub("", name).strip(" -,")
    name = re.sub(r"\s+New$", "", name)
    name = re.sub(r"\s*-?\s*\bClass [A-Z]\b.*$", "", name).strip(" -,")
    for _ in range(2):
        name = LEGAL.sub("", name).strip(" -,")
    if cls:
        name += f" (Class {cls.group(1)})"
    return name[:60]


def parse(text, kind):
    rows = []
    lines = text.splitlines()
    for line in lines[1:]:
        p = line.split("|")
        if len(p) < 7 or line.startswith("File Creation"):
            continue
        if kind == "nasdaq":
            sym, name, test = p[0], p[1], p[3]
        else:
            sym, name, test = p[0], p[1], p[6]
        if test == "Y" or "$" in sym or re.search(r"\.(W|WS|U|R|WI|WD|CL)$", sym):
            continue
        if SKIP_NAME.search(name):
            continue
        sym = sym.strip().upper().replace(".", "-")           # BRK.B is BRK-B on Yahoo
        if not TICKER_RE.match(sym):
            continue
        rows.append((sym, clean_name(name) or sym))
    return rows


def build(out=OUT):
    seen = {}
    for kind, url in SOURCES.items():
        r = requests.get(url, timeout=40, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        for sym, name in parse(r.text, kind):
            seen.setdefault(sym, name)
    if len(seen) < 3000:                                        # a broken download must not wipe the list
        raise RuntimeError(f"only {len(seen)} tickers parsed - keeping the old list")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"#{dt.date.today().isoformat()}\n")
        for sym in sorted(seen):
            f.write(f"{sym}|{seen[sym]}\n")
    return len(seen)


def refresh_if_stale(out=OUT, max_age_days=7):
    try:
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                stamp = f.readline().strip().lstrip("#")
            if (dt.date.today() - dt.date.fromisoformat(stamp)).days < max_age_days:
                return False
        n = build(out)
        print("ticker list rebuilt:", n, "symbols")
        return True
    except Exception as e:
        print("ticker list not refreshed:", e)
        return False


if __name__ == "__main__":
    print("wrote", build(), "tickers to", OUT)
    sys.exit(0)
