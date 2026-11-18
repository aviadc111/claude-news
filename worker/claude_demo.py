"""
Claude's demo account: which stocks it holds
--------------------------------------------
The app's Demo has two accounts with pretend money: the user's own, and "Claude's". Claude's
account does not think: it follows one fixed, public recipe, the "copy an investor" method
described by the Trader skill (github.com/Jakeschincariol/trader-skill), written here from
that description:

  1. take the investor's newest quarterly report of holdings (form 13F-HR) from the SEC
  2. add up the rows of each company (share classes of one company count as one holding);
     options and bonds are left out
  3. keep the largest few and scale their weights to 100
  4. budget rules: a cash buffer comes off the top, and no stock may be more than the cap
     (what is over the cap is shared out to the others by weight)

This file works out those target weights and saves them, with the prices of those stocks, in
data/claude_demo.json. The app does the pretend buying itself, with the same amount and on
the same day as the user's demo, so nothing about the user is here.

The report is old news by design: it shows what the firm held on the last day of a quarter
and is filed up to 45 days later. Nothing here is advice or a forecast.

    python worker/claude_demo.py            show the targets (asks the SEC, saves nothing)
"""

import datetime as dt
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_PATH = os.path.join(ROOT, "data", "claude_demo.json")

# The SEC only answers programs that say who to contact (a name and an email). The contact is
# not written here: it comes from the SEC_USER_AGENT setting (a GitHub secret on the checker).
# Without it the SEC is not asked at all and the targets stay as they are.
def user_agent():
    return (os.environ.get("SEC_USER_AGENT") or "").strip()

DEFAULTS = {"cik": 1067983, "name": "Berkshire Hathaway", "top": 5, "cash_pct": 10, "cap_pct": 25, "check_hours": 6}
# Share classes of one company are one holding. CUSIP -> the ticker the prices come under.
SHARE_CLASSES = {"02079K305": "GOOGL", "02079K107": "GOOGL", "084670108": "BRK-B", "084670702": "BRK-B"}
LEGAL_TAIL = re.compile(r",?\s+(Inc\.?|Corporation|Corp\.?|Ltd\.?|PLC|plc|N\.V\.|S\.A\.|Holdings?|Company|Co\.?|Del|New)\s*$", re.I)


def rules(cfg):
    r = dict(DEFAULTS)
    r.update((cfg or {}).get("claude_demo") or {})
    return r


def load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ the SEC ---
def sec_get(url, get=None):
    ua = user_agent()
    if not ua:
        raise RuntimeError("SEC_USER_AGENT is not set")
    time.sleep(0.25)                                         # far under the SEC's limit of 10 a second
    r = (get or requests.get)(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=30)
    r.raise_for_status()
    return r


def latest_report(cik, get=None):
    """The newest original 13F-HR (not an amendment): accession number, quarter end, filing date."""
    d = sec_get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", get).json()
    r = d["filings"]["recent"]
    for form, acc, filed, period in zip(r["form"], r["accessionNumber"], r["filingDate"], r["reportDate"]):
        if form == "13F-HR":
            return {"accession": acc, "filed": filed, "period": period,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"}
    raise ValueError("no 13F-HR among the recent filings")


def table_xml(report, get=None):
    """The filing's information table: the XML file in its folder that is not the cover page."""
    items = sec_get(report["url"] + "index.json", get).json()["directory"]["item"]
    xmls = [i for i in items if i["name"].lower().endswith(".xml") and "primary_doc" not in i["name"].lower()]
    if not xmls:
        raise ValueError("no information table in the filing folder")
    best = max(xmls, key=lambda i: int(i.get("size") or 0))
    return sec_get(report["url"] + best["name"], get).text


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_table(xml_text):
    """Rows of the information table as dicts: name, cusip, value, kind (SH or PRN), put_call."""
    rows = []
    for el in ET.fromstring(xml_text).iter():
        if _local(el.tag) != "infoTable":
            continue
        row = {"name": "", "cusip": "", "value": 0.0, "kind": "", "put_call": ""}
        for c in el:
            tag, text = _local(c.tag), (c.text or "").strip()
            if tag == "nameOfIssuer":
                row["name"] = text
            elif tag == "cusip":
                row["cusip"] = text.upper()
            elif tag == "value":
                row["value"] = float(text.replace(",", "") or 0)
            elif tag == "putCall":
                row["put_call"] = text
            elif tag == "shrsOrPrnAmt":
                for cc in c:
                    if _local(cc.tag) == "sshPrnamtType":
                        row["kind"] = (cc.text or "").strip().upper()
        rows.append(row)
    return rows


def by_company(rows):
    """Stock rows added up per CUSIP, largest first. Options (PUT / CALL) and bonds (PRN) are left out."""
    total = {}
    for r in rows:
        if r["put_call"] or r["kind"] == "PRN" or not r["cusip"] or r["value"] <= 0:
            continue
        t = total.setdefault(r["cusip"], {"cusip": r["cusip"], "name": r["name"], "value": 0.0})
        t["value"] += r["value"]
    return sorted(total.values(), key=lambda t: -t["value"])


def figi_tickers(cusips, post=None):
    """CUSIP -> US ticker from OpenFIGI's free lookup (no key; ten per request). Unknown ones are left out."""
    out = {}
    cusips = list(cusips)
    for i in range(0, len(cusips), 10):
        part = cusips[i:i + 10]
        for attempt in range(3):                              # the free service is busy now and then
            r = (post or requests.post)("https://api.openfigi.com/v3/mapping", timeout=30,
                                        json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in part])
            if r.status_code < 500 and r.status_code != 429:
                break
            time.sleep(6 * (attempt + 1))
        r.raise_for_status()
        for c, res in zip(part, r.json()):
            data = res.get("data") or []
            common = [d for d in data if d.get("securityType2") == "Common Stock"] or data
            if common and common[0].get("ticker"):
                out[c] = common[0]["ticker"].replace("/", "-").replace(".", "-").upper()
        if i + 10 < len(cusips):
            time.sleep(3)
    return out


def tidy(name):
    """'Coca-Cola Company (The)' -> 'Coca-Cola'."""
    name = re.sub(r"\s*\(The\)\s*$", "", str(name)).strip()
    for _ in range(2):
        name = LEGAL_TAIL.sub("", name).strip()
    return name


def clean_name(issuer):
    name = " ".join(w if w.isupper() and len(w) <= 3 and w not in ("INC", "CO", "DEL", "NEW") else w.capitalize() for w in issuer.split())
    for _ in range(2):
        name = LEGAL_TAIL.sub("", name).strip()
    return name or issuer


# ------------------------------------------------------------------ the maths ---
def top_holdings(companies, tickers, top):
    """The `top` largest holdings with a ticker, share classes combined.
    Each: ticker, name, pct_13f (its share of all the stocks reported), weight (scaled so these sum to 100)."""
    whole = sum(c["value"] for c in companies) or 1.0
    merged = {}
    for c in companies:
        t = tickers.get(c["cusip"])
        if not t:
            continue
        m = merged.setdefault(t, {"ticker": t, "name": clean_name(c["name"]), "value": 0.0})
        m["value"] += c["value"]
    best = sorted(merged.values(), key=lambda m: -m["value"])[:top]
    kept = sum(m["value"] for m in best) or 1.0
    return [{"ticker": m["ticker"], "name": m["name"], "pct_13f": round(m["value"] / whole * 100, 2),
             "weight": round(m["value"] / kept * 100, 2)} for m in best]


def size(weights, cash_pct, cap_pct):
    """Budget rules: {ticker: weight} -> {ticker: percent of the budget}.
    The cash buffer comes off the top; a stock over the cap is pinned at the cap and what was
    over is shared out to the others by weight, again until nobody is over."""
    left = {t: float(w) for t, w in weights.items() if w > 0}
    budget, out = 100.0 - cash_pct, {}
    while left:
        total = sum(left.values())
        share = {t: budget * w / total for t, w in left.items()}
        over = [t for t, v in share.items() if v > cap_pct + 1e-9]
        if not over:
            out.update(share)
            break
        for t in over:
            out[t] = float(cap_pct)
            budget -= cap_pct
            del left[t]
    return {t: round(v, 2) for t, v in out.items()}


def build(cfg, known=None, get=None, post=None):
    """Ask the SEC and work out the targets. `known` is {cusip: ticker} already looked up."""
    r = rules(cfg)
    report = latest_report(r["cik"], get)
    companies = by_company(parse_table(table_xml(report, get)))
    tickers = dict(known or {})
    tickers.update(SHARE_CLASSES)
    need = [c["cusip"] for c in companies[:r["top"] + 8] if c["cusip"] not in tickers]
    if need:
        tickers.update(figi_tickers(need, post))
    holdings = top_holdings(companies, tickers, int(r["top"]))
    if len(holdings) < 2:
        raise ValueError("could not match enough holdings to tickers")
    targets = size({h["ticker"]: h["weight"] for h in holdings}, r["cash_pct"], r["cap_pct"])
    for h in holdings:
        h["target"] = targets.get(h["ticker"], 0.0)
    return {
        "investor": {"name": r["name"], "cik": int(r["cik"])},
        "report": report,
        "rules": {"top": int(r["top"]), "cash_pct": r["cash_pct"], "cap_pct": r["cap_pct"]},
        "holdings": holdings,
        "cusips": {c["cusip"]: tickers[c["cusip"]] for c in companies if c["cusip"] in tickers},
    }


def refresh(cfg, old=None, now=None, get=None, post=None):
    """The targets, asking the SEC at most every `check_hours`. Whatever goes wrong, the old ones stay.
    Returns (data or None, changed)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    r = rules(cfg)
    if old and old.get("holdings") and old.get("checked"):
        try:
            age = now - dt.datetime.fromisoformat(old["checked"])
            if age < dt.timedelta(hours=float(r["check_hours"])) and old.get("investor", {}).get("cik") == int(r["cik"]) \
                    and old.get("rules") == {"top": int(r["top"]), "cash_pct": r["cash_pct"], "cap_pct": r["cap_pct"]}:
                return old, False
        except (TypeError, ValueError):
            pass
    try:
        new = build(cfg, (old or {}).get("cusips"), get, post)
    except Exception as e:
        print("Claude's demo: could not read the investor's report this time:", type(e).__name__, e)
        if old and old.get("holdings"):
            return old, False
        return None, False
    same = bool(old) and (old.get("report") or {}).get("accession") == new["report"]["accession"] and old.get("holdings") == new["holdings"]
    if same:
        old["checked"] = now.isoformat(timespec="seconds")
        return old, True
    first = not (old and old.get("holdings"))
    # "since": the day this report became the one to follow. The app rebalances Claude's account at that day's prices.
    new["since"] = new["report"]["filed"] if first else now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    new["checked"] = now.isoformat(timespec="seconds")
    new["stocks"] = (old or {}).get("stocks", [])
    print(f"Claude's demo: following {new['investor']['name']}'s report for {new['report']['period']} (filed {new['report']['filed']}):",
          ", ".join(f"{h['ticker']} {h['target']}%" for h in new["holdings"]))
    return new, True


def tickers(data):
    return [h["ticker"] for h in (data or {}).get("holdings", [])]


if __name__ == "__main__":
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        d = build(json.load(f), (load() or {}).get("cusips"))
    print(f"{d['investor']['name']}: holdings as of {d['report']['period']}, filed {d['report']['filed']} ({d['report']['accession']})")
    for h in d["holdings"]:
        print(f"  {h['ticker']:<6} {h['name']:<28} {h['pct_13f']:>6.2f}% of the report   weight {h['weight']:>6.2f}   target {h['target']:>5.2f}% of the budget")
    print(f"  cash   {100 - sum(h['target'] for h in d['holdings']):.2f}%")
    sys.exit(0)
