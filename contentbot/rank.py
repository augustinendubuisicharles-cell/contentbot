"""Group items about the same story and rank stories by how widely they're covered."""
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .gather import Item

STOP = set("""a an the and or but of to in on at for from with by as is are was were be been has have had
it its this that these those after before over under into about amid says say said new more than not no
will would could can may up out off what who why how when where his her their they he she we you i
just live latest update updates news report video watch""".split())


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOP}


@dataclass
class Story:
    items: list[Item] = field(default_factory=list)
    words: set[str] = field(default_factory=set)
    score: float = 0.0
    trending: bool = False

    @property
    def lead(self) -> Item:
        news = [i for i in self.items if i.kind == "news"] or self.items
        return max(news, key=lambda i: len(i.summary))

    @property
    def sources(self) -> list[str]:
        return sorted({i.source for i in self.items})


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def cluster(items: list[Item], threshold: float = 0.5) -> list[Story]:
    stories: list[Story] = []
    for item in sorted(items, key=lambda i: i.published, reverse=True):
        words = tokens(item.title)
        if len(words) < 2:
            continue
        best = max(stories, key=lambda s: _similar(words, s.words), default=None)
        if best and _similar(words, best.words) >= threshold:
            best.items.append(item)
            best.words |= words
        else:
            stories.append(Story(items=[item], words=set(words)))
    return stories


def rank(items: list[Item], trends: list[str], top: int = 12) -> list[Story]:
    now = datetime.now(timezone.utc)
    trend_words = [tokens(t) for t in trends]
    stories = cluster(items)
    for s in stories:
        n_sources = len(s.sources)
        engagement = sum(i.engagement for i in s.items)
        newest = max(i.published for i in s.items)
        hours_old = max((now - newest).total_seconds() / 3600, 0)
        s.trending = any(tw and tw <= s.words for tw in trend_words)
        s.score = (
            3.0 * n_sources
            + 1.0 * math.log1p(engagement)
            + (4.0 if s.trending else 0.0)
            - 0.15 * hours_old
        )
    stories.sort(key=lambda s: s.score, reverse=True)
    return stories[:top]
