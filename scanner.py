"""Solana paper scanner. Run once with python scanner.py, or poll with --watch."""
import json
import math
import re
import sqlite3
import sys
import time
from urllib.request import Request, urlopen

API = "https://api.dexscreener.com"
COST = 0.01  # Assumed friction per side, including fees and slippage.
STAKE = 10.0  # Virtual USD, max five open positions.


def fetch(path):
    request = Request(API + path, headers={"User-Agent": "paper-scanner/1.0"})
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError("Unexpected market feed")
    return data


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else 0
    except (TypeError, ValueError):
        return 0


def scan(db):
    now = int(time.time())
    active = dict(db.execute("SELECT token,pool FROM trades WHERE pnl IS NULL"))
    profiles = fetch("/token-profiles/latest/v1")
    tokens = list(active)
    for profile in profiles:
        token = profile.get("tokenAddress", "")
        if profile.get("chainId") == "solana" and re.fullmatch("[1-9A-HJ-NP-Za-km-z]{32,44}", token) and token not in tokens:
            tokens.append(token)
    pools = {}
    for i in range(0, len(tokens), 30):
        rows = fetch("/tokens/v1/solana/" + ",".join(tokens[i:i+30]))
        for row in rows:
            if row.get("chainId") == "solana":
                pools.setdefault(row["baseToken"]["address"], []).append(row)
    with db:
        for token in tokens:
            rows = pools.get(token, [])
            if token in active:
                rows = [row for row in rows if row.get("pairAddress") == active[token]]
            row = max(rows, key=liquidity, default={})
            price = number(row.get("priceUsd"))
            if token in active:
                entry, opened, misses = db.execute("SELECT entry,opened,misses FROM trades WHERE token=?", (token,)).fetchone()
                if price <= 0 or liquidity(row) <= 0:
                    db.execute("UPDATE trades SET misses=misses+1 WHERE token=?", (token,))
                    if misses < 2:
                        continue
                    pnl, reason = -STAKE, "three missing quotes (assumed total loss)"
                else:
                    db.execute("UPDATE trades SET misses=0 WHERE token=?", (token,))
                    pnl = STAKE * (price / entry * (1-COST)**2 - 1)
                    if -1 < pnl < 2 and now-opened < 86400 and liquidity(row) >= 25000:
                        continue
                    reason = "price, liquidity or time exit"
                db.execute("UPDATE trades SET pnl=?,closed=?,reason=? WHERE token=?", (pnl, now, reason, token))
                del active[token]
            elif len(active) < 5 and price > 0 and liquidity(row) >= 100000:
                born = number(row.get("pairCreatedAt")) / 1000
                flow = (row.get("txns") or {}).get("m5") or {}
                change = number((row.get("priceChange") or {}).get("m5"))
                if born > 0 and now-born >= 86400 and number(flow.get("buys")) > number(flow.get("sells")) >= 10 and 0 < change <= 10:
                    result = db.execute("INSERT OR IGNORE INTO trades(token,pool,opened,entry) VALUES(?,?,?,?)", (token,row["pairAddress"],now,price))
                    if result.rowcount:
                        active[token] = row["pairAddress"]
    closed, pnl = db.execute("SELECT count(pnl),coalesce(sum(pnl),0) FROM trades").fetchone()
    print(f"{now} | {len(tokens)} tokens | {len(active)} open | {closed} closed | realized paper P/L USD {pnl:.2f}", flush=True)


def liquidity(row):
    return number((row.get("liquidity") or {}).get("usd"))


if __name__ == "__main__":
    db = sqlite3.connect("paper.sqlite")
    db.execute("""CREATE TABLE IF NOT EXISTS trades(token TEXT PRIMARY KEY, pool TEXT, opened INTEGER,
                  entry REAL, closed INTEGER, pnl REAL, reason TEXT, misses INTEGER DEFAULT 0)""")
    while True:
        try:
            scan(db)
        except (OSError, ValueError, KeyError) as error:
            print(f"Scan skipped: {error}", file=sys.stderr, flush=True)
            if "--watch" not in sys.argv:
                raise
        if "--watch" not in sys.argv:
            break
        time.sleep(60)
