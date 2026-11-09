#!/usr/bin/env python3
"""
Clipping-campaign scanner: ranks live pay-per-view campaigns by expected value.

Reads the PUBLIC campaign discovery listing (no login, no API key, no scraping of
private spaces) and extracts the fields that decide whether a campaign is worth your
time. Two of those fields are the whole game and neither is comparable by eye:

  successRate         % of submissions actually approved. Ranges from ~5% to ~50%+.
                      A $2 CPM campaign that rejects 95% of work pays less than a
                      $0.80 campaign that approves half.
  progressPercentage  how much of the budget is already spent. Clipping into an
                      exhausted pool is unpaid work.

EV per 1,000 views  =  CPM  x  (successRate / 100)  x  budget-remaining-is-real

Usage:
    python3 campaigns.py fetch          # pull the listing, store a snapshot
    python3 campaigns.py rank [n]       # top n by expected value (default 20)
    python3 campaigns.py new            # campaigns that appeared since last snapshot
    python3 campaigns.py show <id>      # full rules/description for one campaign
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "campaigns.db")
SRC = "https://contentrewards.com/discover"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
            experience_id TEXT PRIMARY KEY,
            title TEXT, brand TEXT, category TEXT, campaign_type TEXT,
            price_per_view REAL, total_budget REAL, budget_spent REAL,
            progress REAL, success_rate REAL, view_count TEXT, engagement REAL,
            platforms TEXT, status TEXT, verified INTEGER, route TEXT,
            description TEXT,
            first_seen TEXT, last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            experience_id TEXT, seen_at TEXT,
            budget_spent REAL, total_budget REAL, success_rate REAL, price_per_view REAL,
            PRIMARY KEY (experience_id, seen_at)
        );
    """)
    return c


def money(x):
    """'$$30,000' / '$1.50' / 1.5 -> float"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^0-9.]", "", str(x))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def extract(html):
    """Pull campaign objects out of the server-streamed RSC payload."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', html)
    blob = ""
    for ch in chunks:
        try:
            blob += json.loads(ch)
        except Exception:
            continue

    # Single string-aware forward pass. Track a stack of '{' positions; when an object
    # closes, keep it if it carries whopExperienceId AT THIS LEVEL. Scanning backward from
    # the key instead would land on the nested "stats" object and silently drop everything.
    out, seen = [], set()
    stack, instr, esc = [], False, False
    for i, c in enumerate(blob):
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "{":
            stack.append(i)
        elif c == "}":
            if not stack:
                continue
            start = stack.pop()
            if i - start > 60000:
                continue
            frag = blob[start:i + 1]
            if '"whopExperienceId"' not in frag:
                continue
            try:
                obj = json.loads(frag)
            except Exception:
                continue
            eid = obj.get("whopExperienceId") or obj.get("experienceId")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            out.append(obj)
    return out


def fetch():
    r = subprocess.run(["curl", "-s", "-L", "--max-time", "60", "-A", UA, SRC],
                       capture_output=True, text=True)
    html = r.stdout
    if len(html) < 10000:
        sys.exit(f"listing fetch looks wrong ({len(html)} bytes) — site may have changed")
    rows = extract(html)
    if not rows:
        sys.exit("parsed 0 campaigns — the page structure changed; re-inspect before trusting")
    now = datetime.now(timezone.utc).isoformat()
    c = conn()
    added = 0
    for o in rows:
        eid = o.get("whopExperienceId") or o.get("experienceId")
        stats = o.get("stats") or {}
        vals = (
            o.get("title"), o.get("brand"), o.get("category"), o.get("campaignType"),
            money(o.get("pricePerView")), money(o.get("totalBudget")), money(o.get("budgetSpent")),
            money(o.get("progressPercentage")), money(stats.get("successRate")),
            stats.get("viewCount"), money(stats.get("engagement")),
            ",".join(o.get("socialPlatforms") or []), o.get("status"),
            1 if o.get("isVerified") else 0, o.get("whopProductRoute"),
            (o.get("description") or "")[:6000],
        )
        exists = c.execute("SELECT 1 FROM campaigns WHERE experience_id=?", (eid,)).fetchone()
        if exists:
            c.execute("""UPDATE campaigns SET title=?,brand=?,category=?,campaign_type=?,
                price_per_view=?,total_budget=?,budget_spent=?,progress=?,success_rate=?,
                view_count=?,engagement=?,platforms=?,status=?,verified=?,route=?,description=?,
                last_seen=? WHERE experience_id=?""", vals + (now, eid))
        else:
            added += 1
            c.execute("""INSERT INTO campaigns (title,brand,category,campaign_type,
                price_per_view,total_budget,budget_spent,progress,success_rate,view_count,
                engagement,platforms,status,verified,route,description,first_seen,last_seen,
                experience_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vals + (now, now, eid))
        c.execute("""INSERT OR REPLACE INTO snapshots
            (experience_id,seen_at,budget_spent,total_budget,success_rate,price_per_view)
            VALUES (?,?,?,?,?,?)""",
            (eid, now, vals[6], vals[5], vals[8], vals[4]))
    c.commit()
    total = c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
    print(f"fetched {len(rows)} campaigns from the public listing")
    print(f"  {added} new, {len(rows)-added} updated   |   {total} tracked overall")


def ev(r):
    """Expected $ per 1,000 approved views. None when we can't compute honestly."""
    cpm, sr = r["price_per_view"], r["success_rate"]
    if cpm is None:
        return None
    # unknown success rate: don't invent one, just flag it
    return cpm * ((sr / 100.0) if sr is not None else None) if sr is not None else None


def rank(n=20):
    c = conn()
    rows = [r for r in c.execute("SELECT * FROM campaigns WHERE status='active'")]
    if not rows:
        sys.exit("no campaigns stored — run 'fetch' first")
    scored = []
    for r in rows:
        e = ev(r)
        if e is None:
            continue
        prog = r["progress"] if r["progress"] is not None else 0
        remaining = None
        if r["total_budget"] and r["budget_spent"] is not None:
            remaining = r["total_budget"] - r["budget_spent"]
        scored.append((e, prog, remaining, r))
    # exclude near-exhausted pools: clipping into them is unpaid work
    live = [s for s in scored if s[1] < 90]
    live.sort(key=lambda s: -s[0])
    print(f"{len(rows)} active campaigns | {len(scored)} scoreable | "
          f"{len(live)} with budget left (<90% spent)\n")
    print(f"{'EV/1k':>7} {'CPM':>6} {'ACC%':>5} {'SPENT':>6} {'LEFT':>10}  {'PLATFORMS':<18} TITLE")
    print("-" * 104)
    for e, prog, rem, r in live[:n]:
        left = f"${rem:,.0f}" if rem is not None else "—"
        plats = (r["platforms"] or "")[:17]
        print(f"${e:>6.2f} ${r['price_per_view']:>5.2f} {r['success_rate']:>4.0f}% "
              f"{prog:>5.0f}% {left:>10}  {plats:<18} {(r['title'] or '')[:44]}")
    print()
    print("EV/1k = CPM x acceptance rate. ACC% is the share of submissions approved —")
    print("a high CPM with a 5% acceptance rate is worse than a low CPM at 50%.")
    skipped = len(scored) - len(live)
    if skipped:
        print(f"({skipped} campaigns hidden: >=90% of budget already spent)")
    noacc = sum(1 for r in rows if r["success_rate"] is None and r["price_per_view"] is not None)
    if noacc:
        print(f"({noacc} campaigns have no acceptance rate published — not scored rather than guessed)")


def new():
    c = conn()
    last = c.execute("SELECT MAX(last_seen) m FROM campaigns").fetchone()["m"]
    rows = c.execute(
        "SELECT * FROM campaigns WHERE first_seen = ? ORDER BY price_per_view DESC", (last,)
    ).fetchall()
    if not rows:
        print("no campaigns first seen in the latest fetch")
        return
    print(f"{len(rows)} campaign(s) new in the latest fetch:\n")
    for r in rows:
        print(f"  ${r['price_per_view'] or 0:.2f} CPM  acc {r['success_rate'] or 0:.0f}%  "
              f"{r['title']}")
        print(f"     {r['route']}  |  {r['platforms']}")


def burn(n=20):
    """How fast money is ACTUALLY leaving each budget, between the two latest snapshots.

    This is the only number that says whether a campaign is paying anyone right now.
    Advertised CPM does not.
    """
    c = conn()
    times = [r["seen_at"] for r in c.execute(
        "SELECT DISTINCT seen_at FROM snapshots ORDER BY seen_at DESC LIMIT 2")]
    if len(times) < 2:
        print("only one snapshot so far — run `fetch` again later to measure burn rate")
        return
    now_t, prev_t = times
    days = (datetime.fromisoformat(now_t) - datetime.fromisoformat(prev_t)).total_seconds() / 86400
    rows = c.execute("""
        SELECT b.experience_id eid, c.title, c.route, c.price_per_view cpm, c.success_rate acc,
               a.budget_spent prev, b.budget_spent now,
               (b.total_budget - b.budget_spent) remaining
        FROM snapshots b JOIN snapshots a ON a.experience_id = b.experience_id
        JOIN campaigns c ON c.experience_id = b.experience_id
        WHERE b.seen_at=? AND a.seen_at=? AND b.budget_spent > a.budget_spent
        ORDER BY (b.budget_spent - a.budget_spent) DESC LIMIT ?""", (now_t, prev_t, n)).fetchall()
    total = sum(r["now"] - r["prev"] for r in rows)
    print(f"burn over {days:.2f} days ({prev_t[:16]} -> {now_t[:16]})")
    print(f"{len(rows)} campaigns paid out ${total:,.0f} = ${total/days:,.0f}/day "
          f"shared across ALL clippers on the platform\n")
    print(f"{'$/day':>8}  {'runway':>7}  {'CPM':>5} {'acc':>4}  campaign")
    for r in rows:
        rate = (r["now"] - r["prev"]) / days
        runway = f"{r['remaining']/rate:.0f}d" if rate > 0 else "-"
        print(f"{rate:8,.0f}  {runway:>7}  {r['cpm'] or 0:5.2f} {r['acc'] or 0:3.0f}%  {r['title'][:44]}")


def show(eid):
    c = conn()
    r = c.execute("SELECT * FROM campaigns WHERE experience_id=? OR route=?", (eid, eid)).fetchone()
    if not r:
        sys.exit(f"no campaign matching '{eid}'")
    print(f"{r['title']}\n")
    print(f"  brand         {r['brand']}")
    print(f"  category      {r['category']}   type {r['campaign_type']}")
    print(f"  CPM           ${r['price_per_view'] or 0:.2f} per 1,000 views")
    print(f"  acceptance    {r['success_rate'] or 0:.0f}% of submissions approved")
    print(f"  budget        ${r['total_budget'] or 0:,.0f} total, "
          f"${r['budget_spent'] or 0:,.0f} spent ({r['progress'] or 0:.0f}%)")
    print(f"  platforms     {r['platforms']}")
    print(f"  verified      {'yes' if r['verified'] else 'no'}")
    print(f"  route         {r['route']}")
    print(f"\n--- RULES / BRIEF ---\n{r['description']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "fetch":
        fetch()
    elif cmd == "rank":
        rank(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    elif cmd == "new":
        new()
    elif cmd == "burn":
        burn(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    elif cmd == "show":
        show(sys.argv[2])
    else:
        print(__doc__)
