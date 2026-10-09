"""Collect the day's stories from RSS feeds, Reddit, X and Google Trends."""
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from dateutil import parser as dateparser

from .config import env

log = logging.getLogger(__name__)
UA = {"User-Agent": "ContentBot/1.0 (daily news summary)"}
TIMEOUT = 20


@dataclass
class Item:
    title: str
    summary: str
    url: str
    source: str
    published: datetime
    kind: str = "news"          # news | reddit | x
    engagement: int = 0         # upvotes / likes, when known
    tags: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateparser.parse(value) if isinstance(value, str) else value
    except (ValueError, OverflowError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_rss(urls: list[str], since: datetime) -> list[Item]:
    items = []
    for url in urls:
        try:
            resp = requests.get(url, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except requests.RequestException as e:
            log.warning("RSS %s failed: %s", url, e)
            continue
        source = _clean(feed.feed.get("title", url))
        for entry in feed.entries:
            published = _parse_time(entry.get("published") or entry.get("updated"))
            if published and published < since:
                continue
            items.append(Item(
                title=_clean(entry.get("title", "")),
                summary=_clean(entry.get("summary", ""))[:600],
                url=entry.get("link", ""),
                source=source,
                published=published or datetime.now(timezone.utc),
            ))
    return items


def from_reddit(subreddits: list[str], since: datetime) -> list[Item]:
    items = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=25"
        try:
            resp = requests.get(url, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            posts = resp.json()["data"]["children"]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("Reddit r/%s failed: %s", sub, e)
            continue
        for post in posts:
            d = post["data"]
            published = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
            if published < since or d.get("stickied"):
                continue
            items.append(Item(
                title=_clean(d["title"]),
                summary=_clean(d.get("selftext", ""))[:400],
                url=d.get("url_overridden_by_dest") or f"https://reddit.com{d['permalink']}",
                source=f"r/{sub}",
                published=published,
                kind="reddit",
                engagement=int(d.get("score", 0)),
            ))
    return items


def from_x(queries: list[str], since: datetime, min_likes: int = 0) -> list[Item]:
    token = env("X_BEARER_TOKEN")
    if not token:
        log.info("X_BEARER_TOKEN not set; skipping X")
        return []
    items = []
    for q in queries:
        try:
            resp = requests.get(
                "https://api.x.com/2/tweets/search/recent",
                headers={"Authorization": f"Bearer {token}", **UA},
                params={
                    "query": q,
                    "max_results": 100,
                    "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tweet.fields": "created_at,public_metrics,author_id",
                    "sort_order": "relevancy",
                },
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            tweets = resp.json().get("data", [])
        except (requests.RequestException, ValueError) as e:
            log.warning("X query %r failed: %s", q, e)
            continue
        for t in tweets:
            likes = t.get("public_metrics", {}).get("like_count", 0)
            if likes < min_likes:
                continue
            text = _clean(t["text"])
            items.append(Item(
                title=text[:140],
                summary=text,
                url=f"https://x.com/i/web/status/{t['id']}",
                source="X",
                published=_parse_time(t.get("created_at")) or datetime.now(timezone.utc),
                kind="x",
                engagement=likes,
            ))
    return items


def trending_terms(geo: str) -> list[str]:
    try:
        resp = requests.get(f"https://trends.google.com/trending/rss?geo={geo}", headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        return [_clean(e.title).lower() for e in feedparser.parse(resp.content).entries]
    except requests.RequestException as e:
        log.warning("Google Trends failed: %s", e)
        return []


def gather(cfg: dict, lookback_hours: int) -> tuple[list[Item], list[str]]:
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    src = cfg["sources"]
    items = from_rss(src.get("rss", []), since)
    items += from_reddit(src.get("reddit", []), since)
    x = src.get("x") or {}
    items += from_x(x.get("queries", []), since, x.get("min_likes", 0))
    trends = trending_terms(src.get("trends", {}).get("geo", "US"))
    items = [i for i in items if i.title]
    log.info("Gathered %d items and %d trending terms", len(items), len(trends))
    return items, trends
