"""Turn ranked stories into a one-minute narrated script plus per-platform captions."""
import logging

import anthropic
from pydantic import BaseModel, Field

from .config import env
from .rank import Story

log = logging.getLogger(__name__)


class ScriptError(Exception):
    pass


class Segment(BaseModel):
    headline: str = Field(description="On-screen headline, max 8 words")
    narration: str = Field(description="What the presenter says, 20-30 words, plain spoken English")
    image_query: str = Field(description="2-4 word search for an openly licensed photo, e.g. 'European Parliament building'. Prefer places, objects or public figures, never graphic content")
    sources: list[str] = Field(description="Outlets that reported it")


class Captions(BaseModel):
    youtube_title: str = Field(description="Under 90 characters, curiosity-driven but accurate, no clickbait")
    youtube_description: str
    facebook: str
    instagram: str = Field(description="Hook on the first line, short story list, call to follow")
    first_comment_question: str = Field(description="One open question to start discussion in comments")


class Script(BaseModel):
    intro: str = Field(description="Opening line, max 15 words, includes the greeting and date")
    segments: list[Segment]
    outro: str = Field(description="Closing line, max 15 words, asks viewers to follow for the next edition")
    captions: Captions
    hashtags: list[str] = Field(description="Relevant hashtags without the # sign, mix of broad and story-specific")


PROMPT = """You are the writer for "{brand}", a {edition} news video report posted as a YouTube Short, Facebook Reel and Instagram Reel.
Date: {date}. Greeting: "{greeting}".

Pick the {n} most important and interesting stories below (prefer stories covered by several outlets, and those marked TRENDING), and write:
- a spoken script that fits in {seconds} seconds total (about {words} words across intro, segments and outro),
- facts exactly as reported: never state anything that isn't in the material, and attribute contested claims ("Reuters reports..."),
- captions for each platform, and up to {max_tags} hashtags.

Voice and tone: {style}
Read the room: stories involving deaths, violence, disasters, abuse or serious illness are told straight and with respect, with no jokes about them or the people affected. Save the humour for the lighter stories, the absurd details, and the links between segments. Never mock someone for who they are.

Stories (ranked):
{stories}"""


def _story_block(stories: list[Story]) -> str:
    lines = []
    for idx, s in enumerate(stories, 1):
        lead = s.lead
        flag = " [TRENDING]" if s.trending else ""
        lines.append(
            f"{idx}.{flag} {lead.title}\n   Sources: {', '.join(s.sources)}\n   {lead.summary[:400]}\n   {lead.url}"
        )
    return "\n".join(lines)


def write_script(stories: list[Story], cfg: dict, edition: str, date_str: str) -> Script:
    n = cfg["video"]["stories"]
    if env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN"):
        try:
            return _claude_script(stories, cfg, edition, date_str)
        except (anthropic.APIError, ScriptError) as e:
            log.warning("Claude script failed (%s); using headline fallback", e)
    else:
        log.info("No Anthropic credentials; using headline fallback script")
    return _fallback_script(stories[:n], cfg, edition, date_str)


def _claude_script(stories: list[Story], cfg: dict, edition: str, date_str: str) -> Script:
    vcfg, wcfg = cfg["video"], cfg["writer"]
    prompt = PROMPT.format(
        brand=cfg["brand"]["name"],
        edition=edition,
        date=date_str,
        greeting=cfg["editions"][edition]["greeting"],
        style=wcfg.get("style", "Clear and neutral."),
        n=vcfg["stories"],
        seconds=vcfg["max_seconds"] - 4,
        words=int((vcfg["max_seconds"] - 4) * 2.5),
        max_tags=cfg["growth"]["max_hashtags"],
        stories=_story_block(stories),
    )
    # Keys that aren't scoped to a workspace need the workspace named on each request.
    workspace = env("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
    response = client.messages.parse(
        model=wcfg["model"],
        max_tokens=16000,
        output_config={"effort": wcfg.get("effort", "low")},
        messages=[{"role": "user", "content": prompt}],
        output_format=Script,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ScriptError(f"no script (stop_reason={response.stop_reason})")
    script = response.parsed_output
    script.segments = script.segments[: vcfg["stories"]]
    return script


def _fallback_script(stories: list[Story], cfg: dict, edition: str, date_str: str) -> Script:
    greeting = cfg["editions"][edition]["greeting"]
    brand = cfg["brand"]["name"]
    segments = []
    for s in stories:
        lead = s.lead
        words = lead.title.split()
        narration = lead.title.rstrip(".") + "."
        if len(words) < 18 and lead.summary:
            extra = " ".join(lead.summary.split()[: 26 - len(words)])
            narration += " " + extra.rstrip(".,;:") + "."
        segments.append(Segment(
            headline=" ".join(words[:8]),
            narration=narration,
            image_query=" ".join(sorted(s.words, key=len, reverse=True)[:3]),
            sources=s.sources[:3],
        ))
    titles = "\n".join(f"• {seg.headline}" for seg in segments)
    tags = ["news", "dailynews", "breakingnews", "worldnews", "shorts"]
    return Script(
        intro=f"{greeting}, here's your {brand} for {date_str}.",
        segments=segments,
        outro="Follow for your next update.",
        captions=Captions(
            youtube_title=f"{brand} {edition.title()} | {date_str}",
            youtube_description=f"Today's top stories:\n{titles}",
            facebook=f"{brand}, {date_str}\n{titles}",
            instagram=f"Your {edition} news in 60 seconds\n{titles}\nFollow for daily updates.",
            first_comment_question="Which of these stories matters most to you?",
        ),
        hashtags=tags,
    )
