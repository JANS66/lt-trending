#!/usr/bin/env python3
"""Show trending (most popular) YouTube videos in Lithuania, per category.

Usage:
    export YOUTUBE_API_KEY=...        # https://console.cloud.google.com -> YouTube Data API v3
    python3 lt_trending.py                    # all categories, top 10 each, in terminal
    python3 lt_trending.py -n 25              # top 25 per category
    python3 lt_trending.py -c Music -c Gaming # only these categories (name or id)
    python3 lt_trending.py --html report.html # also write a clickable HTML report
    python3 lt_trending.py --json out.json    # also write raw results as JSON
    python3 lt_trending.py --region LV        # any other country code works too
    python3 lt_trending.py -r LT -r PL        # several countries in one report
    python3 lt_trending.py --neighbours       # LT plus its neighbours, closest to Vilnius first
    python3 lt_trending.py --include-shorts   # keep YouTube Shorts (excluded by default)

Stdlib only, no pip install needed. Each run costs ~1 quota unit per category
per country (default daily quota is 10,000 units), plus ~1 more per extra page
fetched to replace filtered-out Shorts.
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

API = "https://www.googleapis.com/youtube/v3"

# Countries bordering Lithuania, closest to furthest by capital-to-Vilnius distance (km).
NEIGHBOURS = [("BY", 172), ("LV", 262), ("PL", 391), ("RU", 790)]
COUNTRY_NAMES = {"LT": "Lithuania", "BY": "Belarus", "LV": "Latvia", "PL": "Poland", "RU": "Russia"}


def api_get(endpoint, key, **params):
    params["key"] = key
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)["error"]
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            raise ApiError(e.code, reason, err.get("message", ""))
        except (ValueError, KeyError):
            raise ApiError(e.code, "", str(e))


class ApiError(Exception):
    def __init__(self, code, reason, message):
        super().__init__(f"HTTP {code} {reason}: {message}")
        self.code, self.reason = code, reason


def get_categories(key, region, lang):
    data = api_get("videoCategories", key, part="snippet", regionCode=region, hl=lang)
    return [
        {"id": c["id"], "title": c["snippet"]["title"]}
        for c in data.get("items", [])
        if c["snippet"].get("assignable")
    ]


SHORTS_MAX_SECONDS = 180  # Shorts can be up to 3 minutes long


def is_short(video_id):
    """Ask youtube.com: /shorts/<id> serves Shorts directly and redirects other videos
    to /watch. Returns None if the answer is inconclusive (network error, consent page...)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}", method="HEAD",
                                 headers={"User-Agent": "Mozilla/5.0", "Cookie": "SOCS=CAI"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400 and "/watch" in (e.headers.get("Location") or ""):
            return False
        return None
    except (urllib.error.URLError, OSError):
        return None


def drop_shorts(videos):
    """Remove Shorts: short videos that are vertical (from the API's embed size). Only square
    ones are ambiguous; those get checked against youtube.com, which is slow (~1.5s each)."""
    def verdict(v):
        if not 0 < v["seconds"] <= SHORTS_MAX_SECONDS:
            return False
        if "#shorts" in v["title"].lower():
            return True
        w, h = v["embed_size"]
        if w and h and w != h:
            return h > w
        return is_short(v["id"]) is not False  # inconclusive -> trust the duration
    with ThreadPoolExecutor(max_workers=16) as pool:
        shorts = list(pool.map(verdict, videos))
    return [v for v, short in zip(videos, shorts) if not short]


def get_trending(key, region, category_id, max_results, include_shorts=False):
    params = dict(part="snippet,statistics,contentDetails,player", maxWidth=1000, chart="mostPopular",
                  regionCode=region, maxResults=50 if not include_shorts else min(max_results, 50))
    if category_id:
        params["videoCategoryId"] = category_id
    videos = []
    while len(videos) < max_results:
        data = api_get("videos", key, **params)
        page = [parse_video(v) for v in data.get("items", [])]
        videos += page if include_shorts else drop_shorts(page)
        if not data.get("nextPageToken"):
            break
        params["pageToken"] = data["nextPageToken"]
    return videos[:max_results]


def parse_video(v):
    s, st = v["snippet"], v.get("statistics", {})
    iso = v.get("contentDetails", {}).get("duration", "")
    return {
        "id": v["id"],
        "title": s["title"],
        "channel": s["channelTitle"],
        "published": s["publishedAt"],
        "views": int(st.get("viewCount", 0)),
        "likes": int(st["likeCount"]) if "likeCount" in st else None,
        "duration": fmt_duration(iso),
        "seconds": duration_seconds(iso),
        "embed_size": (int(v.get("player", {}).get("embedWidth", 0)),
                       int(v.get("player", {}).get("embedHeight", 0))),
        "thumb": s.get("thumbnails", {}).get("medium", {}).get("url", ""),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    }


def parse_iso_duration(iso):
    # PT1H2M3S -> (1, 2, 3); None if not a plain PT duration (e.g. P0D for live)
    if not iso.startswith("PT"):
        return None
    h = m = s = 0
    num = ""
    for ch in iso[2:]:
        if ch.isdigit():
            num += ch
        else:
            if ch == "H": h = int(num)
            elif ch == "M": m = int(num)
            elif ch == "S": s = int(num)
            num = ""
    return h, m, s


def duration_seconds(iso):
    hms = parse_iso_duration(iso)
    return hms[0] * 3600 + hms[1] * 60 + hms[2] if hms else 0


def fmt_duration(iso):
    # PT1H2M3S -> 1:02:03
    hms = parse_iso_duration(iso)
    if not hms:
        return ""
    h, m, s = hms
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def fmt_ago(iso):
    """'2026-10-02T14:00:00Z' -> '5 days ago' (or 'N hours ago' if under a day)."""
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    secs = max((datetime.now(timezone.utc) - then).total_seconds(), 0)
    days, hours = int(secs // 86400), int(secs // 3600)
    if days:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return "just now"


def fmt_num(n):
    if n is None:
        return "-"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


def region_label(r):
    dist = f", {r['distance_km']} km from Vilnius" if r["distance_km"] else ""
    return f"{r['name']} ({r['region']}{dist})"


def print_results(regions):
    for r in regions:
        print(f"\n##### {region_label(r)} #####")
        if r.get("note"):
            print(f"  (skipped: {r['note']})")
        print_categories(r["categories"])


def print_categories(results):
    for cat in results:
        print(f"\n=== {cat['title']} (id {cat['id']}) ===")
        if not cat["videos"]:
            print(f"  (no trending chart: {cat.get('note', 'empty')})")
            continue
        for i, v in enumerate(cat["videos"], 1):
            title = v["title"] if len(v["title"]) <= 70 else v["title"][:67] + "..."
            print(f"  {i:>2}. {title}")
            print(f"      {v['channel']} | {fmt_num(v['views'])} views | {v['duration']} | {v['url']}")


def write_html(regions, path, generated):
    blocks, region_nav = [], []
    for r in regions:
        region_nav.append(f'<a href="#{r["region"]}">{html.escape(r["name"])}</a>')
        if r.get("note"):
            body = f'<section><p class="empty">Skipped: {html.escape(r["note"])}.</p></section>'
        else:
            body = html_categories(r["categories"], r["region"])
        blocks.append(f'<div class="region" id="{r["region"]}"><h1>{html.escape(region_label(r))}</h1>'
                      f'{body}</div>')
    title = ", ".join(r["region"] for r in regions)

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trending in {title}</title><style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--card:#f4f4f5;--acc:#c00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f0f10;--fg:#eee;--mut:#999;--card:#1c1c1f;--acc:#f55}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}}
header{{padding:20px 16px 8px}} header h1{{margin:0;font-size:22px}} header p{{margin:4px 0;color:var(--mut)}}
header nav{{position:static;border:0;padding:8px 0 0}}
.region h1{{margin:0;padding:20px 16px 4px;font-size:20px;border-top:2px solid var(--card)}}
nav{{position:sticky;top:0;background:var(--bg);padding:8px 16px;display:flex;gap:6px;overflow-x:auto;border-bottom:1px solid var(--card);z-index:1}}
nav a{{white-space:nowrap;padding:4px 10px;border-radius:99px;background:var(--card);color:var(--fg);text-decoration:none;font-size:13px}}
nav a span{{color:var(--mut)}}
section{{padding:8px 16px}} h2{{font-size:18px;border-left:4px solid var(--acc);padding-left:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.card{{color:inherit;text-decoration:none}} .card:hover h3{{color:var(--acc)}}
.th{{position:relative}} .th img{{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:var(--card)}}
.th b{{position:absolute;top:6px;left:6px;background:#000c;color:#fff;padding:1px 7px;border-radius:4px;font-size:12px}}
.th i{{position:absolute;bottom:8px;right:6px;background:#000c;color:#fff;padding:1px 5px;border-radius:4px;font-size:12px;font-style:normal}}
.card h3{{font-size:14px;margin:6px 0 2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.card p{{margin:0;color:var(--mut);font-size:12px}} .empty{{color:var(--mut)}}
</style></head><body>
<header><h1>YouTube trending in {title}, by category</h1><p>Generated {generated}</p>
{f'<nav>{"".join(region_nav)}</nav>' if len(regions) > 1 else ''}</header>
{''.join(blocks)}</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def html_categories(results, region):
    sections, nav = [], []
    for cat in results:
        anchor = f"{region}-cat-{cat['id']}"
        nav.append(f'<a href="#{anchor}">{html.escape(cat["title"])} '
                   f'<span>{len(cat["videos"])}</span></a>')
        if cat["videos"]:
            cards = "".join(
                f'<a class="card" href="{v["url"]}" target="_blank" rel="noopener">'
                f'<div class="th"><img loading="lazy" src="{html.escape(v["thumb"])}" alt="">'
                f'<b>{i}</b><i>{v["duration"]}</i></div>'
                f'<h3>{html.escape(v["title"])}</h3>'
                f'<p>{html.escape(v["channel"])}</p>'
                f'<p>{fmt_num(v["views"])} views · {fmt_num(v["likes"])} likes · '
                f'<span title="{v["published"][:10]}">{fmt_ago(v["published"])}</span></p></a>'
                for i, v in enumerate(cat["videos"], 1))
        else:
            cards = f'<p class="empty">No trending chart for this category ({html.escape(cat.get("note", "empty"))}).</p>'
        sections.append(f'<section id="{anchor}"><h2>{html.escape(cat["title"])}</h2>'
                        f'<div class="grid">{cards}</div></section>')

    return f'<nav>{"".join(nav)}</nav>{"".join(sections)}'


def main():
    ap = argparse.ArgumentParser(description="YouTube trending videos per category for a country.")
    ap.add_argument("-k", "--key", default=os.environ.get("YOUTUBE_API_KEY"),
                    help="YouTube Data API v3 key (default: $YOUTUBE_API_KEY)")
    ap.add_argument("-r", "--region", action="append",
                    help="ISO country code, repeatable (default: LT)")
    ap.add_argument("--neighbours", action="store_true",
                    help="also include Lithuania's neighbours, closest to Vilnius first: "
                         + ", ".join(c for c, _ in NEIGHBOURS))
    ap.add_argument("--lang", default="en", help="language for category names, e.g. lt (default: en)")
    ap.add_argument("-n", "--max", type=int, default=10, help="videos per category, max 50 (default: 10)")
    ap.add_argument("-c", "--category", action="append",
                    help="only this category name or id (repeatable)")
    ap.add_argument("--include-shorts", action="store_true",
                    help="keep YouTube Shorts (excluded by default)")
    ap.add_argument("--no-overall", action="store_true", help="skip the overall (all categories) chart")
    ap.add_argument("--html", metavar="FILE", help="write an HTML report")
    ap.add_argument("--json", metavar="FILE", help="write results as JSON")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't print to terminal")
    args = ap.parse_args()

    if not args.key:
        sys.exit("No API key. Set YOUTUBE_API_KEY or pass --key. "
                 "Get one at https://console.cloud.google.com (enable 'YouTube Data API v3').")

    distances = dict(NEIGHBOURS, LT=0)
    codes = [c.upper() for c in args.region or ["LT"]]
    if args.neighbours:
        codes += [c for c, _ in NEIGHBOURS]
    codes = list(dict.fromkeys(codes))  # dedupe, keep order

    regions = []
    for code in codes:
        r = {"region": code, "name": COUNTRY_NAMES.get(code, code),
             "distance_km": distances.get(code), "categories": []}
        try:
            cats = get_categories(args.key, code, args.lang)
        except ApiError as e:
            if len(codes) == 1:
                sys.exit(f"Could not load categories: {e}")
            r["note"] = f"could not load categories ({e})"
            print(f"{code}: {r['note']}", file=sys.stderr)
            regions.append(r)
            continue
        if args.category:
            wanted = {c.lower() for c in args.category}
            cats = [c for c in cats if c["id"] in wanted or c["title"].lower() in wanted]
            if not cats:
                r["note"] = "no matching categories"
        if not args.no_overall and not args.category:
            cats.insert(0, {"id": "", "title": "Overall (all categories)"})
        r["categories"] = cats
        regions.append(r)
    if args.category and all(r.get("note") for r in regions):
        sys.exit("No matching categories. Run without -c to see the available ones.")

    def fetch(job):
        region, cat = job
        entry = dict(cat, videos=[])
        try:
            entry["videos"] = get_trending(args.key, region, cat["id"], args.max,
                                            args.include_shorts)
        except ApiError as e:
            if e.code in (400, 403) and e.reason in ("quotaExceeded", "keyInvalid", "forbidden"):
                raise
            # Many categories have no chart in a given region -> API returns 404 notFound.
            entry["note"] = e.reason or f"HTTP {e.code}"
        if not args.quiet:
            print(f"  fetched {region} {cat['title']}: {len(entry['videos'])} videos", file=sys.stderr)
        return entry

    jobs = [(r["region"], cat) for r in regions for cat in r["categories"]]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = iter(pool.map(fetch, jobs))
            for r in regions:
                r["categories"] = [next(fetched) for _ in r["categories"]]
    except ApiError as e:
        sys.exit(f"API error: {e}")

    # categories with content first (keeping Overall on top), empty ones last
    for r in regions:
        r["categories"].sort(key=lambda c: (c["id"] != "", not c["videos"]))

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not args.quiet:
        print_results(regions)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"generated": generated, "regions": regions},
                      f, ensure_ascii=False, indent=2)
        print(f"\nJSON written to {args.json}", file=sys.stderr)
    if args.html:
        write_html(regions, args.html, generated)
        print(f"HTML report written to {args.html}", file=sys.stderr)

if __name__ == "__main__":
    main()
