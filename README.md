# Clip Campaign Ranker

Clipping platforms pay per view for short clips of streamers. They advertise rates loudly and payouts quietly, so this ranks public pay-per-view campaigns on money that has actually moved instead of the advertised rate. `campaigns.py` pulls the public campaign listing, stores a snapshot each run and ranks campaigns by expected value (rate × approval rate, only while budget is left). Snapshots show how fast each campaign is spending its budget. When I ran it, 34 campaigns showing $106k available had paid out $79 between them, and a high advertised rate turned out to be a mild warning sign.

`discord_watch.py` is a smaller piece of the same research: it reads campaign announcements that a Discord bot sees in your own server.

## Run it

Python 3 (standard library only) and curl.

```
python3 campaigns.py fetch     # pull the public listing and store a snapshot
python3 campaigns.py rank 20   # top 20 by expected value
python3 campaigns.py burn      # how fast each campaign is spending
python3 campaigns.py new       # campaigns that appeared since the last snapshot
```

I ran `fetch` once a day from cron. Snapshots go into a local `campaigns.db`, which isn't in the repo. For `discord_watch.py`, run `python3 discord_watch.py setup` for the steps, then put your own bot token in `DISCORD_BOT_TOKEN` or a `.discord_token` file.

## Status

It reads the public listing only: no login and no private spaces. The parser depends on how the site's page is built, so it can break when the site changes.

Portfolio: https://thejungjulian-del.github.io/portfolio/
