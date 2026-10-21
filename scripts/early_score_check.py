#!/usr/bin/env python3
"""Early score check on the OrcaLayer API: does "the price moved his way after he traded" carry over to the next period,
and if it does, is it foresight or just a cheaper entry price?

This is the method of research issue 4 (a negative result), run on the OrcaLayer public API instead of our internal database:
  1. Read every trade of $1 or more (the API minimum) in the markets of data/markets.csv: 2,000 resolved markets (not sports,
     not short Up / Down markets) still open on July 1, 2026 by their scheduled end date, ranked by the entries the write-up
     found in them in May-June 2026 only (nothing from the check period goes into the choice), most first. Rows with
     an exchange or adapter contract on either side are skipped, so every fill is counted once, with its two real wallets.
  2. An entry = a wallet's NET position change in a market over one UTC day: shares of YES (bought YES or sold NO) minus
     shares of NO. The larger side keeps its share-weighted price, $ = net shares x that price, entries from $100. A same-day
     round trip nets to zero, and so does buying both outcomes.
  3. Price after H (H = 1 h and 24 h) = the $-weighted price of OTHER wallets' trades in the latest hour of
     (entry hour, hour of entry + H] in which they traded at least $100. If there is no such hour and nobody traded the market
     after it, the market's final price. Move = (price after - entry price) x 100, in cents, for the side the wallet holds.
  4. Score on period A' (default May-June 2026): weight w = min($, 10,000) / 1,000, mean move m = sum(w x move) / sum(w),
     n_eff = sum(w)^2 / sum(w^2), M = m x n_eff / (n_eff + 5). |M| < 1 cent is neutral (score 50); otherwise the score is the
     percentile of M among all A' wallets, 0-49 below the band, 51-100 above. Top 10% = score >= 90, bottom 10% = score <= 10.
  5. The same wallets in period B' (default July-September 2026): the 24 h move, the 1 h move and the drift from 1 h to 24 h
     (if the move is already there after one hour, what carries over is the entry price, not foresight), and the result of
     holding each entry to settlement (cents per entry and $ per $10 staked, no fee). t: standard errors clustered by event
     (data/markets.csv has the Polymarket event id; a market without one is its own cluster).

Usage
  export ORCALAYER_API_KEY=sk_orca_...        # Premium key, https://orcalayer.com/docs/api
  python3 early_score_check.py --csv entries.csv                   # first 1,000 markets (about 11,700 requests); rerun it to continue
  python3 early_score_check.py --max-markets 500 --csv e500.csv    # first 500 markets (about 9,500 requests)
  python3 early_score_check.py --load-csv entries.csv              # rerun the analysis, no API calls

Differences from the write-up: the write-up scored wallets on October 2025 - March 2026 (period A) and checked them on
April - September 2026 (period B), on every trade in our database. The API's trade history starts on May 1, 2026, so this
script uses shorter periods (A' May-June, B' July-September 2026) and only the markets with the most entries in A': a random
sample of markets has too few wallets that trade in both periods. On this list every group gains in B', all wallets included
(longer "by a date" markets drift towards the side most wallets hold), so compare the groups with each other and with all
wallets, not with zero. Expect numbers that are similar, not identical. Wallet labels such as
"Smart Money" are not used. Only the Python 3.9+ standard library is used.
"""
import argparse
import bisect
import calendar
import collections
import csv
import gzip
import hashlib
import http.client
import json
import math
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://orcalayer.com"
KEY_ENV = "ORCALAYER_API_KEY"
HERE = os.path.dirname(os.path.abspath(__file__))
# Polymarket exchange and adapter contracts. They show up as a side of routed and aggregated orders and are not traders;
# a row with one of them on either side is skipped. Matched by address prefix (look them up on Polygonscan).
EXCLUDED_PREFIXES = ("0x4bfb41d5", "0xc5d563a3", "0xe1111800", "0xe2222d27", "0xe3333700", "0x127ad3a6")
SIDE_YES, SIDE_NO = "token1", "token2"   # how the API names a market's first and second outcome
SHARES = "token_amount"                  # the API field with the number of shares in a fill
MIN_USD = 100.0          # smallest entry, and the least $ of other wallets' trades in an hour that gives a price
K = 5.0                  # shrinkage: a wallet with 5 effective entries keeps half of its mean move
BAND = 1.0               # |M| below 1 cent = neutral (score 50)
HORIZONS = (1, 24)
# columns of an entry row (and of the --csv file)
COLS = ["wallet", "market_id", "event", "day", "expo_yes", "usd", "p_in", "last_ts", "move_1h", "move_24h", "final_yes"]
WALLET, MARKET, EVENT, DAY, EXPO, USD, P_IN, LAST, M1, M24, FINAL = range(len(COLS))
REQUESTS = [0]


class DailyLimit(Exception):
    pass


def get(path, params, key, tries=6):
    url = API + path + "?" + urllib.parse.urlencode(params)
    waits = 0
    i = 0
    while i < tries:
        req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "orcalayer-early-score-check/1.0"})
        REQUESTS[0] += 1
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("Retry-After") or 5)
                if wait > 900:                                    # the daily request limit, not the per-minute one
                    raise DailyLimit("daily request limit reached, resets in %.0f min" % (wait / 60))
                waits += 1
                if waits > 30:
                    raise RuntimeError("rate limited too long: " + url)
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                sys.exit("API key rejected (%d). A Premium key is required: https://orcalayer.com/docs/api" % e.code)
            if e.code >= 500 and i < tries - 1:
                time.sleep(2 * (i + 1))
                i += 1
                continue
            raise
        except (OSError, http.client.HTTPException, ValueError):   # timeouts, resets, cut or non-JSON responses
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                i += 1
                continue
            raise
    raise RuntimeError("too many retries: " + url)


def day_ts(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d"))


def month_bounds(ts):
    t = time.gmtime(ts)
    y, m = t.tm_year, t.tm_mon
    return calendar.timegm((y, m, 1, 0, 0, 0)), calendar.timegm((y + m // 12, m % 12 + 1, 1, 0, 0, 0))


def load_markets(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="") as f:
        return list(csv.DictReader(f))


def final_price(m, key, a):
    """The market's final YES price from /markets/resolved (slug prefix + end date), or None if it is not resolved."""
    prefix = m["slug"][:40]
    params = {"slug_prefix": prefix, "limit": 500, "offset": 0}
    if m.get("end_date"):
        params["end_from"] = params["end_to"] = m["end_date"]
    while True:
        d = get("/api/public/v1/markets/resolved", params, key)
        time.sleep(a.pause)
        page = d.get("markets", [])
        for r in page:
            if r.get("condition_id") == m["condition_id"]:
                fp = r.get("final_price_yes")
                return fp if fp is not None and (fp >= 0.99 or fp <= 0.01) else None
        if not page or not d.get("has_more"):
            return None
        params["offset"] += len(page)


def read_trades(cid, key, a, lo, hi):
    """Every trade of $1+ in the market with lo <= timestamp < hi, newest first; also whether the page cap cut it short."""
    params = {"wallets": "all", "min_usd": 1, "limit": 500, "before": hi - 1}
    trades, pages = [], 0
    while True:
        d = get("/api/public/v1/market/%s/whale-trades" % cid, params, key)
        time.sleep(a.pause)
        page = d.get("trades", [])
        trades += page
        pages += 1
        if not d.get("has_more") or not page or page[-1]["timestamp"] < lo:
            return trades, False
        if a.max_pages and pages >= a.max_pages:
            return trades, True
        params["before"], params["before_id"] = d["next_before"], d["next_before_id"]


def market_entries(trades, final_yes, t_start, t_end):
    """API trades of one market -> entries [wallet, day, expo_yes, usd, p_in, last_ts, move_1h, move_24h] (rules above)."""
    lo, hi = t_start - 86400, t_end + 90000
    hour_usd, hour_p = collections.defaultdict(float), collections.defaultdict(float)
    own = collections.defaultdict(lambda: [0.0, 0.0])           # (wallet, hour) -> [$, $ x YES price] of its own trades
    days = {}                                                     # (wallet, day) -> [YES shares, x price, NO shares, x price, last ts]
    stamps, seen = [], set()
    for t in trades:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        ts, side = int(t["timestamp"]), t.get("side")
        usd, sh = float(t.get("usd_amount") or 0), float(t.get(SHARES) or 0)
        mk, tk = (t.get("maker") or "").lower(), (t.get("taker") or "").lower()
        if not lo <= ts < hi or side not in (SIDE_YES, SIDE_NO) or usd <= 0 or sh <= 0 or not mk or not tk:
            continue
        if mk.startswith(EXCLUDED_PREFIXES) or tk.startswith(EXCLUDED_PREFIXES):
            continue
        p1 = float(t["price"]) if side == SIDE_YES else 1.0 - float(t["price"])
        h = ts // 3600
        stamps.append(ts)
        hour_usd[h] += usd
        hour_p[h] += usd * p1
        for w, d in ((mk, t.get("maker_direction")), (tk, t.get("taker_direction"))):
            o = own[(w, h)]
            o[0] += usd
            o[1] += usd * p1
            if not t_start <= ts < t_end:
                continue
            r = days.setdefault((w, ts // 86400), [0.0, 0.0, 0.0, 0.0, 0])
            r[4] = max(r[4], ts)
            if d not in ("BUY", "SELL"):
                continue
            if (d == "BUY") == (side == SIDE_YES):                # bought YES or sold NO = YES exposure
                r[0] += sh
                r[1] += sh * p1
            else:
                r[2] += sh
                r[3] += sh * (1.0 - p1)
    hours = sorted(hour_usd)
    stamps.sort()
    out = []
    for (w, day), (sy, py, sn, pn, last) in days.items():
        yes = sy >= sn
        held = sy if yes else sn
        if held <= 0:
            continue
        p_in = (py if yes else pn) / held
        usd = abs(sy - sn) * p_in
        if usd < MIN_USD:
            continue
        # "nobody traded the market after that hour" is judged inside the entry month's window (the write-up ran month by month)
        m_end = month_bounds(day * 86400)[1]
        mlast = stamps[bisect.bisect_left(stamps, m_end + 90000) - 1] // 3600
        moves = []
        for H in HORIZONS:
            h0, h1 = last // 3600, (last + H * 3600) // 3600
            p1 = None
            i = bisect.bisect_right(hours, h1) - 1
            while i >= 0 and hours[i] > h0:
                h = hours[i]
                o = own.get((w, h), (0.0, 0.0))
                if hour_usd[h] - o[0] >= MIN_USD:
                    p1 = (hour_p[h] - o[1]) / (hour_usd[h] - o[0])
                    break
                i -= 1
            if p1 is None and final_yes is not None and mlast <= h1:
                p1 = final_yes
            moves.append(None if p1 is None else ((p1 if yes else 1.0 - p1) - p_in) * 100.0)
        out.append([w, day, yes, usd, p_in, last] + moves)
    return out


def download(a, key):
    """Read the markets through the API -> entry rows. With --csv every market is saved as soon as it is read, and a rerun
    of the same command continues where the last one stopped (e.g. after the daily request limit)."""
    t_start, t_end = day_ts(a.a_start), day_ts(a.b_end)
    lo, hi = t_start - 86400, t_end + 90000
    markets = load_markets(a.markets)                             # the file is sorted by the write-up's entries, most first
    if a.max_markets:
        markets = markets[:a.max_markets]
    rows, done, out, log = [], set(), None, None
    # Wallets are kept as salted hashes, in memory and in the --csv file: the analysis only needs to tell wallets apart, and a
    # saved file should not be a ready list of addresses. The salt is random, made on the first run and kept in <csv>.salt,
    # so a resumed run gives the same hashes.
    salt = secrets.token_hex(32)
    if a.csv:
        sp = a.csv + ".salt"
        if os.path.exists(a.csv) and os.path.exists(a.csv + ".done") and not os.path.exists(sp):
            sys.exit("%s has no %s next to it, so its wallets cannot be matched; start a new --csv file" % (a.csv, sp))
        if not os.path.exists(sp):
            with open(sp, "w") as f:
                f.write(salt)
        with open(sp) as f:
            salt = f.read().strip()
    tag = lambda w: hashlib.sha256((salt + w).encode()).hexdigest()[:16]     # noqa: E731
    if a.csv:
        if os.path.exists(a.csv) and os.path.exists(a.csv + ".done"):
            rows = load_csv(a.csv)
            with open(a.csv + ".done") as f:
                done = {x.strip() for x in f if x.strip()}
            print("resuming %s: %d markets done, %d entries" % (a.csv, len(done), len(rows)), file=sys.stderr)
        else:
            with open(a.csv, "w", newline="") as f:
                csv.writer(f).writerow(COLS)
            open(a.csv + ".done", "w").close()
        out, log = open(a.csv, "a", newline=""), open(a.csv + ".done", "a")
    todo = [m for m in markets if m["market_id"] not in done]
    print("markets to read: %d of the first %d in %s" % (len(todo), len(markets), a.markets), file=sys.stderr)
    stat = collections.Counter()
    try:
        for i, m in enumerate(todo):
            fy = final_price(m, key, a)
            new = []
            if fy is None:
                stat["not_resolved"] += 1
            else:
                trades, cut = read_trades(m["condition_id"], key, a, lo, hi)
                start = t_start
                if cut:                                           # keep only whole days after the oldest trade read
                    stat["cut"] += 1
                    start = max(t_start, (min(t["timestamp"] for t in trades) // 86400 + 1) * 86400)
                stat["read"] += 1
                stat["trades"] += len(trades)
                for e in market_entries(trades, fy, start, t_end):
                    new.append([tag(e[0]), m["market_id"], m.get("event_id") or "m" + m["market_id"]] + e[1:] + [fy])
            rows += new
            if out:
                write_rows(csv.writer(out), new)
                out.flush()
                log.write(m["market_id"] + "\n")
                log.flush()
            if (i + 1) % 50 == 0:
                print("  %d / %d markets, %d entries, %d requests" % (i + 1, len(todo), len(rows), REQUESTS[0]), file=sys.stderr)
    except DailyLimit as e:
        print("stopped: %s. The report below covers the markets read so far;%s" % (
            e, " rerun the same command after the reset to continue." if a.csv else " use --csv to be able to continue."),
            file=sys.stderr)
    print("markets read now %d (cut by --max-pages %d, not resolved %d), trades %d, API requests %d" % (
        stat["read"], stat["cut"], stat["not_resolved"], stat["trades"], REQUESTS[0]))
    return rows


# ----------------------------------------------------------------------------------------------------------------- analysis

def weight(usd):
    return min(usd, 10000.0) / 1000.0


def avg_ranks(v):
    order = sorted(range(len(v)), key=v.__getitem__)
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(a, b):
    n = len(a)
    if n < 5:
        return float("nan")
    ra, rb = avg_ranks(a), avg_ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else float("nan")


def per_wallet(entries, value):
    """wallet -> [n, sum w, sum w x value, sum w^2] over entries where value(e) is not None."""
    s = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for e in entries:
        x = value(e)
        if x is None:
            continue
        w = weight(e[USD])
        r = s[e[WALLET]]
        r[0] += 1
        r[1] += w
        r[2] += w * x
        r[3] += w * w
    return s


def scores(entries_a):
    """Early score of every A' wallet: shrunk mean 24 h move M, neutral band, percentile."""
    s = per_wallet(entries_a, lambda e: e[M24])
    ws = list(s)
    M = []
    for w in ws:
        n, W, S, W2 = s[w]
        neff = W * W / W2
        M.append(S / W * neff / (neff + K))
    r = avg_ranks(M)
    out = {}
    for w, m, rk in zip(ws, M, r):
        sc = min(int((rk - 0.5) / len(M) * 100.0), 100)
        sc = 50 if abs(m) < BAND else (max(51, sc) if m > 0 else min(49, sc))
        out[w] = (m, sc, s[w][0])
    return out


def clustered(xs):
    """xs = [(w, value, event)] -> weighted mean, t (standard errors clustered by event), mean without the 5 largest events."""
    ev = collections.defaultdict(lambda: [0.0, 0.0])
    for w, x, k in xs:
        ev[k][0] += w
        ev[k][1] += w * x

    def est(groups):
        W = sum(g[0] for g in groups)
        if not W:
            return float("nan"), float("nan")
        mu = sum(g[1] for g in groups) / W
        se = math.sqrt(sum((g[1] - mu * g[0]) ** 2 for g in groups)) / W
        return mu, (mu / se if se else float("nan"))
    groups = list(ev.values())
    mu, t = est(groups)
    rest = sorted(groups, key=lambda g: -g[0])[5:]
    return mu, t, est(rest)[0], len(ev)


def settle(e):
    """(cents per entry, $ per $10) when the entry is held to settlement; None if the market has no final outcome."""
    f, p = e[FINAL], e[P_IN]
    if f is None or not 0 < p < 1 or 0.01 < f < 0.99:
        return None
    y = 1.0 if f >= 0.99 else 0.0
    if not e[EXPO]:
        y = 1.0 - y
    return 100.0 * (y - p), 10.0 * (y / p - 1.0)


def fmt(x, spec="%+.2f"):
    return "n/a" if x is None or x != x else spec % x


def report(rows, a):
    t_mid, t_end, t_start = day_ts(a.b_start), day_ts(a.b_end), day_ts(a.a_start)
    ea = [e for e in rows if t_start <= e[DAY] * 86400 < t_mid]
    eb = [e for e in rows if t_mid <= e[DAY] * 86400 < t_end]
    sc = scores(ea)
    n_a = len(sc)
    top = {w for w, (m, s, n) in sc.items() if s >= 90}
    bottom = {w for w, (m, s, n) in sc.items() if s <= 10}
    neutral = {w for w, (m, s, n) in sc.items() if s == 50}
    print("\nentries ($%d+ net position per wallet, market and UTC day): A' %s .. %s %d (%d with a 24 h price, %d wallets), "
          "B' %s .. %s %d (%d with a 24 h price)" % (
              MIN_USD, a.a_start, a.b_start, len(ea), sum(e[M24] is not None for e in ea), n_a, a.b_start, a.b_end, len(eb),
              sum(e[M24] is not None for e in eb)))
    if not n_a:
        print("no scored wallets in A'")
        return
    print("A' Early score (K = %g, neutral band +-%g cent): top 10%% (score >= 90) %d wallets, bottom 10%% (<= 10) %d, "
          "neutral (50) %.0f%%, other %d" % (K, BAND, len(top), len(bottom), 100.0 * len(neutral) / n_a,
                                             n_a - len(top) - len(bottom) - len(neutral)))
    groups = [("top 10% of A'", top), ("neutral in A'", neutral), ("bottom 10% of A'", bottom), ("all B' wallets", None)]
    print("\nPeriod B', same wallets. Move = price of the side held after H, from other wallets' trades, minus the entry price,")
    print("cents per entry, weighted by min($, 10k) / 1k. t: standard errors clustered by event.")
    print("%-17s %8s %8s %7s %9s %6s %9s %9s %6s %7s %9s" % (
        "group", "wallets", "entries", "events", "24h move", "t", "w/o top5", "1h move", "t", "24h/1h", "drift"))
    for name, ws in groups:
        g = [e for e in eb if ws is None or e[WALLET] in ws]
        g24 = [(weight(e[USD]), e[M24], e[EVENT]) for e in g if e[M24] is not None]
        g1 = [(weight(e[USD]), e[M1], e[EVENT]) for e in g if e[M1] is not None]
        gd = [(weight(e[USD]), e[M24] - e[M1], e[EVENT]) for e in g if e[M1] is not None and e[M24] is not None]
        if not g24:
            print("%-17s %8d %8d   (no entries with a 24 h price)" % (name, 0, 0))
            continue
        mu24, t24, mu24x, nev = clustered(g24)
        mu1, t1, _, _ = clustered(g1) if g1 else (float("nan"),) * 4
        mud = clustered(gd)[0] if gd else float("nan")
        ratio = mu24 / mu1 if mu1 == mu1 and abs(mu1) >= 0.05 else float("nan")
        print("%-17s %8d %8d %7d %9s %6s %9s %9s %6s %7s %9s" % (
            name, len({e[WALLET] for e in g if e[M24] is not None}), len(g24), nev, fmt(mu24), fmt(t24, "%+.1f"), fmt(mu24x),
            fmt(mu1), fmt(t1, "%+.1f"), fmt(ratio, "%.2f"), fmt(mud)))
    print("\nHeld to settlement (B' entries with a 24 h price in resolved markets; win = 1, loss = 0, minus the entry price; no fee).")
    print("%-17s %8s %11s %6s %11s %9s" % ("group", "entries", "cents/entry", "t", "$ per $10", "avg price"))
    for name, ws in groups:
        g = [(e, settle(e)) for e in eb if (ws is None or e[WALLET] in ws) and e[M24] is not None]
        g = [(e, s) for e, s in g if s is not None]
        if not g:
            continue
        mu, t, _, _ = clustered([(weight(e[USD]), s[0], e[EVENT]) for e, s in g])
        per10 = sum(e[USD] * s[1] for e, s in g) / sum(e[USD] for e, s in g)
        avg_p = sum(weight(e[USD]) * e[P_IN] for e, s in g) / sum(weight(e[USD]) for e, s in g)
        print("%-17s %8d %11s %6s %11s %9.3f" % (name, len(g), fmt(mu), fmt(t, "%+.1f"), fmt(per10, "%+.3f"), avg_p))
    # rank correlations A' -> B' (wallets with >= 3 entries in each period)
    b24 = per_wallet(eb, lambda e: e[M24])
    both = [(sc[w][0], b24[w][2] / b24[w][1]) for w in sc if sc[w][2] >= 3 and w in b24 and b24[w][0] >= 3]
    print("\nRank correlation (Spearman) A' -> B', wallets with >= 3 entries in each period:")
    print("  Early score M (A') vs mean 24 h move (B'): %s, %d wallets" % (fmt(spearman([x for x, _ in both], [y for _, y in both]), "%+.3f"), len(both)))
    has_both = lambda e: e[M1] is not None and e[M24] is not None          # noqa: E731
    for label, val in (("first-hour move", lambda e: e[M1] if has_both(e) else None),
                       ("drift 1 h -> 24 h", lambda e: e[M24] - e[M1] if has_both(e) else None)):
        pa, pb = per_wallet(ea, val), per_wallet(eb, val)
        xy = [(pa[w][2] / pa[w][1], pb[w][2] / pb[w][1]) for w in pa if pa[w][0] >= 3 and w in pb and pb[w][0] >= 3]
        print("  %-17s A' vs B' (exploratory): %s, %d wallets" % (label, fmt(spearman([x for x, _ in xy], [y for _, y in xy]), "%+.3f"), len(xy)))


def load_csv(path):
    num = lambda v: None if v == "" else float(v)                     # noqa: E731
    with open(path, newline="") as f:
        return [[r["wallet"], r["market_id"], r["event"], int(r["day"]), r["expo_yes"] == "1", float(r["usd"]),
                 float(r["p_in"]), int(r["last_ts"]), num(r["move_1h"]), num(r["move_24h"]), num(r["final_yes"])]
                for r in csv.DictReader(f)]


def write_rows(cw, rows):
    """Full float precision, so an analysis of the saved file (--load-csv, a resumed run) equals the one of a straight run."""
    blank = lambda x: "" if x is None else x                     # noqa: E731
    for e in rows:
        cw.writerow([e[WALLET], e[MARKET], e[EVENT], e[DAY], int(e[EXPO]), e[USD], e[P_IN], e[LAST], blank(e[M1]), blank(e[M24]),
                     blank(e[FINAL])])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--markets", default=os.path.join(HERE, "..", "data", "markets.csv"), help="market list (default data/markets.csv)")
    ap.add_argument("--max-markets", type=int, default=1000, help="read the first N markets of the list, the ones with the most "
                    "entries in A' in the write-up (default 1000; 0 = all)")
    ap.add_argument("--a-start", default="2026-05-01", help="first day of score period A' (API trade data starts 2026-05-01)")
    ap.add_argument("--b-start", default="2026-07-01", help="first day of check period B' (= end of A')")
    ap.add_argument("--b-end", default="2026-10-01", help="day after the end of B'")
    ap.add_argument("--max-pages", type=int, default=400, help="pages of 500 trades per market at most; a longer market keeps "
                    "only the whole days after the oldest trade read (0 = no cap)")
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between requests (default 0.15)")
    ap.add_argument("--csv", help="save one row per entry (wallet as a salted hash, market, day, price, moves, outcome; the salt "
                    "goes to <file>.salt) to this file as the "
                    "markets are read; rerunning the same command continues an interrupted run")
    ap.add_argument("--load-csv", help="skip the API and rerun the analysis on entries saved earlier with --csv")
    a = ap.parse_args()
    print("Early score check: score period A' %s .. %s, check period B' %s .. %s (end dates exclusive)" % (
        a.a_start, a.b_start, a.b_start, a.b_end))
    if a.load_csv:
        rows = load_csv(a.load_csv)
        print("entries loaded from %s: %d" % (a.load_csv, len(rows)))
    else:
        key = os.environ.get(KEY_ENV)
        if not key:
            sys.exit("Set %s (Premium key from https://orcalayer.com/docs/api). Never commit it." % KEY_ENV)
        rows = download(a, key)
    report(rows, a)


if __name__ == "__main__":
    main()
