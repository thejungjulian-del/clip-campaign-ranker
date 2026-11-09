#!/usr/bin/env python3
"""
Discord campaign watcher — reads YOUR OWN server and parses campaign postings.

Architecture, and why it's this shape:

    streamer/campaign server
      └─ #announcements  (an "Announcement" channel)
            │  you press Follow  →  Discord cross-posts every published message
            ▼
    YOUR server  →  #feeds  ←  your bot reads this, with your bot token
                                 (a bot in your own server: fully sanctioned)

This never touches anyone else's server and never automates a user account. Automating a
Discord *user* account (a "self-bot") is explicitly prohibited, actively detected, and gets
accounts terminated — which would cost you membership of the servers you need to be in.
Following announcement channels gets the same data with none of that risk.

Uses the REST API by polling, not the gateway: stdlib only, no dependencies, and one
channel poll per minute is far inside the rate limits.

Setup (once):
    python3 discord_watch.py setup      # prints the exact steps
Then:
    python3 discord_watch.py channels   # list channels your bot can see
    python3 discord_watch.py poll       # read new messages, parse, store
    python3 discord_watch.py rank       # campaign postings ranked by what they pay
    python3 discord_watch.py watch 60   # poll on a loop every 60s
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "campaigns.db")          # shares the db with campaigns.py
TOKEN_FILE = os.path.join(HERE, ".discord_token")  # gitignored; you create this yourself
CFG = os.path.join(HERE, "discord_channels.json")
API = "https://discord.com/api/v10"


def token():
    t = os.environ.get("DISCORD_BOT_TOKEN")
    if not t and os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
    if not t:
        sys.exit(
            "No bot token found.\n"
            f"  Put it in {TOKEN_FILE} yourself (one line), or export DISCORD_BOT_TOKEN.\n"
            "  Do not paste it into a chat window — it is a credential.\n"
            "  Run 'setup' for the full walkthrough."
        )
    return t


def api(path, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token()}",
            "User-Agent": "DiscordBot (https://localhost, 1.0)",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait + 0.5, 30))
                continue
            if e.code == 401:
                sys.exit("401 Unauthorized — the bot token is wrong or was regenerated.")
            if e.code == 403:
                sys.exit(f"403 Forbidden — the bot lacks access to {path}.\n"
                         "  Give it View Channel + Read Message History on that channel.")
            sys.exit(f"HTTP {e.code} on {path}: {body}")
        except Exception as e:
            if attempt == 4:
                sys.exit(f"request failed on {path}: {e}")
            time.sleep(2 * (attempt + 1))
    return None


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS discord_posts (
            message_id TEXT PRIMARY KEY,
            channel_id TEXT, channel_name TEXT,
            author TEXT, origin TEXT,
            content TEXT,
            cpm REAL, budget REAL, platforms TEXT, links TEXT,
            posted_at TEXT, seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS discord_cursor (
            channel_id TEXT PRIMARY KEY, last_message_id TEXT
        );
    """)
    return c


# ---------------------------------------------------------------- parsing
# Campaign postings are free text, so parse conservatively: pull a number only when the
# wording is unambiguous, and leave it null otherwise rather than guessing.
CPM_PATTERNS = [
    r"\$\s*([\d]+(?:\.\d+)?)\s*(?:per|/)\s*1[,.]?000\s*views",
    r"\$\s*([\d]+(?:\.\d+)?)\s*(?:per|/)\s*1\s*k\b",
    r"\$\s*([\d]+(?:\.\d+)?)\s*cpm",
    r"cpm\s*(?:of|:|=)?\s*\$\s*([\d]+(?:\.\d+)?)",
    r"rate\s*:?\s*\$\s*([\d]+(?:\.\d+)?)\s*cpm",
]
BUDGET_PATTERNS = [
    r"budget\s*(?:of|:|=)?\s*\$\s*([\d][\d,]*(?:\.\d+)?)\s*(k\b)?",
    r"\$\s*([\d][\d,]*(?:\.\d+)?)\s*(k\b)?\s*budget",
    r"\$\s*([\d][\d,]*(?:\.\d+)?)\s*(k\b)?\s*(?:prize\s*)?pool",
    # amount written after the word: "bounty pool $1,200", "prize pool of $5k"
    r"(?:prize|bounty|reward)?\s*pool\s*(?:of|:|=)?\s*\$\s*([\d][\d,]*(?:\.\d+)?)\s*(k\b)?",
]
PLATFORMS = ("tiktok", "instagram", "reels", "youtube", "shorts", "x.com", "twitter")


def num(s, kflag=None):
    try:
        v = float(str(s).replace(",", ""))
    except ValueError:
        return None
    if kflag:
        v *= 1000
    return v


def parse(text):
    low = text.lower()
    cpm = None
    for p in CPM_PATTERNS:
        m = re.search(p, low)
        if m:
            cpm = num(m.group(1))
            break
    budget = None
    for p in BUDGET_PATTERNS:
        m = re.search(p, low)
        if m:
            budget = num(m.group(1), m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            break
    plats = sorted({p for p in PLATFORMS if p in low})
    links = re.findall(r"https?://[^\s<>\"')]+", text)[:6]
    return cpm, budget, ",".join(plats), ",".join(links)


def looks_like_campaign(text, cpm):
    if cpm is not None:
        return True
    low = text.lower()
    hits = sum(
        1 for k in ("clip", "clipper", "cpm", "per 1000", "per 1k", "payout", "budget",
                    "reward", "bounty", "submit", "campaign")
        if k in low
    )
    return hits >= 3


# ---------------------------------------------------------------- commands
def setup():
    print(__doc__)
    print("""
STEP BY STEP
------------
1. Make your own Discord server (free): Discord → '+' → Create My Own.
   Add a text channel called  #feeds

2. Create the bot:
   discord.com/developers/applications → New Application
   → Bot tab → Reset Token → copy it
   → SCROLL DOWN and enable  MESSAGE CONTENT INTENT   (required to read text)

3. Store the token yourself — do NOT paste it into a chat:
     printf '%s' 'YOUR_TOKEN_HERE' > .discord_token
     chmod 600 .discord_token

4. Invite the bot to YOUR server only:
   OAuth2 → URL Generator → scopes: bot
   → permissions: View Channels, Read Message History
   → open the generated URL, pick your server

5. Follow the sources. In each streamer/campaign server, open their
   #announcements channel. If it shows a 'Follow' button, click it and
   point it at your #feeds. Cross-posted messages now land in your server.

   NOTE: only 'Announcement'-type channels can be followed. A regular text
   channel cannot be read automatically by anything you're allowed to run —
   for those, the public listing that campaigns.py already scans is the
   better source, and it carries acceptance rates Discord posts don't.

6. Then:
     python3 discord_watch.py channels     # confirm the bot sees #feeds
     python3 discord_watch.py watch 60     # poll every 60s
""")


def channels():
    guilds = api("/users/@me/guilds") or []
    if not guilds:
        print("The bot is not in any server yet — finish step 4 of setup.")
        return
    found = []
    for g in guilds:
        print(f"\nserver: {g.get('name')}  (id {g.get('id')})")
        chans = api(f"/guilds/{g['id']}/channels") or []
        for ch in chans:
            if ch.get("type") in (0, 5):  # text, announcement
                kind = "announcement" if ch.get("type") == 5 else "text"
                print(f"   #{ch.get('name'):<26} {kind:<13} id {ch.get('id')}")
                found.append({"id": ch["id"], "name": ch.get("name")})
    if found:
        json.dump(found, open(CFG, "w"), indent=1)
        print(f"\nwrote {len(found)} channel(s) to {os.path.basename(CFG)} — edit it to keep only what you want")


def poll(quiet=False):
    if not os.path.exists(CFG):
        sys.exit("no channel list — run 'channels' first")
    chans = json.load(open(CFG))
    c = conn()
    total_new = 0
    for ch in chans:
        cur = c.execute("SELECT last_message_id FROM discord_cursor WHERE channel_id=?",
                        (ch["id"],)).fetchone()
        params = {"limit": "100"}
        if cur and cur["last_message_id"]:
            params["after"] = cur["last_message_id"]
        msgs = api(f"/channels/{ch['id']}/messages", params) or []
        msgs = sorted(msgs, key=lambda m: int(m["id"]))
        newest = cur["last_message_id"] if cur else None
        for m in msgs:
            body = m.get("content") or ""
            # cross-posted announcements carry their text in embeds as often as content
            for emb in m.get("embeds") or []:
                body += "\n" + " ".join(
                    str(emb.get(k) or "") for k in ("title", "description")
                )
                for f in emb.get("fields") or []:
                    body += f"\n{f.get('name','')}: {f.get('value','')}"
            cpm, budget, plats, links = parse(body)
            if looks_like_campaign(body, cpm):
                origin = ""
                ref = m.get("message_reference") or {}
                if ref.get("guild_id"):
                    origin = f"guild:{ref['guild_id']}"
                c.execute(
                    "INSERT OR REPLACE INTO discord_posts (message_id,channel_id,channel_name,"
                    "author,origin,content,cpm,budget,platforms,links,posted_at,seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m["id"], ch["id"], ch.get("name"),
                     (m.get("author") or {}).get("username"), origin, body[:6000],
                     cpm, budget, plats, links, m.get("timestamp"),
                     datetime.now(timezone.utc).isoformat()),
                )
                total_new += 1
            newest = m["id"]
        if newest:
            c.execute("INSERT OR REPLACE INTO discord_cursor (channel_id,last_message_id) "
                      "VALUES (?,?)", (ch["id"], newest))
        c.commit()
    if not quiet or total_new:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {total_new} new campaign post(s) "
              f"across {len(chans)} channel(s)")
    return total_new


def rank():
    c = conn()
    rows = c.execute(
        "SELECT * FROM discord_posts ORDER BY (cpm IS NULL), cpm DESC, posted_at DESC LIMIT 40"
    ).fetchall()
    if not rows:
        print("nothing stored yet. Run 'poll' after following some announcement channels.")
        print("Meanwhile the public listing has far more: python3 campaigns.py rank")
        return
    print(f"{'CPM':>7} {'BUDGET':>10}  {'PLATFORMS':<24} {'WHEN':<11} SOURCE")
    print("-" * 96)
    for r in rows:
        cpm = f"${r['cpm']:.2f}" if r["cpm"] is not None else "—"
        bud = f"${r['budget']:,.0f}" if r["budget"] is not None else "—"
        when = (r["posted_at"] or "")[:10]
        first = (r["content"] or "").strip().splitlines()
        head = first[0][:34] if first else ""
        print(f"{cpm:>7} {bud:>10}  {(r['platforms'] or '—'):<24} {when:<11} "
              f"#{r['channel_name']} · {head}")
    print("\nCPM/budget are parsed from free text — blank means the wording was ambiguous")
    print("and nothing was guessed. Open the post to confirm before committing work.")


def watch(interval):
    print(f"polling every {interval}s — ctrl-c to stop")
    try:
        while True:
            poll(quiet=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "setup":
        setup()
    elif cmd == "channels":
        channels()
    elif cmd == "poll":
        poll()
    elif cmd == "rank":
        rank()
    elif cmd == "watch":
        watch(int(sys.argv[2]) if len(sys.argv) > 2 else 60)
    else:
        print(__doc__)
