"""Legitimate audience growth: hashtags, first comments, credits, and a performance report.

Deliberately absent: fake accounts, bought or automated likes/views/follows,
and automated commenting on other people's posts. Those break YouTube and Meta
rules and get accounts restricted or banned.
"""
import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DATA
from .publish import meta, youtube

log = logging.getLogger(__name__)
POSTS_LOG = DATA / "posts.jsonl"


def hashtags(tags: list[str], limit: int, always: tuple[str, ...] = ("news",)) -> list[str]:
    seen, out = set(), []
    for t in list(always) + tags:
        t = re.sub(r"[^A-Za-z0-9_]", "", t.lstrip("#"))
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


def tag_line(tags: list[str]) -> str:
    return " ".join(f"#{t}" for t in tags)


def first_comment(question: str, sources: list[str]) -> str:
    return f"{question}\n\nSources: {', '.join(sources)}"


def credits_block(credits: list[str]) -> str:
    credits = [c for c in credits if c]
    return "Images:\n" + "\n".join(f"- {c}" for c in credits) if credits else ""


def log_post(record: dict) -> None:
    POSTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def _load_posts() -> list[dict]:
    if not POSTS_LOG.exists():
        return []
    return [json.loads(l) for l in POSTS_LOG.read_text().splitlines() if l.strip()]


def collect_stats() -> list[dict]:
    """Fetch current views/likes for every logged post. Returns one row per platform post."""
    rows = []
    posts = _load_posts()
    yt_ids = [p["post_id"] for p in posts if p["platform"] == "youtube" and p.get("ok")]
    yt = youtube.stats(yt_ids) if yt_ids and youtube.configured() else {}
    for p in posts:
        if not p.get("ok"):
            continue
        stats = {}
        try:
            if p["platform"] == "youtube":
                stats = yt.get(p["post_id"], {})
            elif p["platform"] == "instagram" and meta.instagram_configured():
                stats = meta.instagram_insights(p["post_id"])
            elif p["platform"] == "facebook" and meta.facebook_configured():
                stats = meta.facebook_reel_insights(p["post_id"])
        except Exception as e:  # one bad post shouldn't stop the report
            log.warning("Stats for %s %s failed: %s", p["platform"], p["post_id"], e)
        rows.append({**{k: p[k] for k in ("posted_at", "edition", "platform", "post_id", "url", "title")},
                     "views": stats.get("views", 0), "likes": stats.get("likes", 0),
                     "comments": stats.get("comments", 0)})
    return rows


def report(tz: str) -> Path:
    """Write data/stats.csv and data/report.md: what's working and the best hour to post."""
    rows = collect_stats()
    DATA.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(DATA / "stats.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    by = defaultdict(list)
    for r in rows:
        local = datetime.fromisoformat(r["posted_at"]).astimezone(ZoneInfo(tz))
        by[(r["platform"], r["edition"], local.hour)].append(r["views"])
    lines = [f"# ContentBot performance ({len(rows)} posts)", "",
             "| Platform | Edition | Hour posted | Posts | Avg views |", "|---|---|---|---|---|"]
    for (platform, edition, hour), views in sorted(by.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        lines.append(f"| {platform} | {edition} | {hour:02d}:00 | {len(views)} | {sum(views) / len(views):.0f} |")
    top = sorted(rows, key=lambda r: r["views"], reverse=True)[:5]
    if top:
        lines += ["", "## Top posts", *[f"- {r['views']} views, {r['platform']}: [{r['title']}]({r['url']})" for r in top]]
    lines += ["", "Move the schedule in .github/workflows/post.yml toward the hours with the highest average views."]
    out = DATA / "report.md"
    out.write_text("\n".join(lines) + "\n")
    return out
