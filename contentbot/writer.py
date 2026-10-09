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
    headline: str = Field(description="On-screen headline, max 7 words, punchy, can tease rather than summarise")
    narration: str = Field(description="What the presenter says, 18-28 words. First sentence is a hook (a surprising fact, number or twist); then what happened and, in a few words, why it matters to the viewer")
    image_query: str = Field(description="2-4 word search for an openly licensed photo, e.g. 'European Parliament building'. Prefer places, objects or public figures, never graphic content")
    sources: list[str] = Field(description="Outlets that reported it")


class Captions(BaseModel):
    youtube_title: str = Field(description="Under 70 characters. Leads with the most intriguing story as a curiosity gap, accurate, no clickbait")
    youtube_description: str
    facebook: str
    instagram: str = Field(description="First line is a scroll-stopping hook, then a short story list, then a call to follow")
    first_comment_question: str = Field(description="One open, slightly playful question that invites opinions in the comments")


class Script(BaseModel):
    hook: str = Field(description="Spoken cold open, max 14 words, said before any greeting. The single most surprising fact of the day, or a tease of the 'and finally' story, phrased so people must keep watching. No 'hello', no 'welcome'")
    hook_text: str = Field(description="On-screen version of the hook, max 6 words, big and bold")
    welcome: str = Field(description="Max 9 words straight after the hook: greeting, show name, day. e.g. 'Good morning, it's Friday and this is Daily Brief.'")
    segments: list[Segment] = Field(description="Most important story first. The LAST segment is the 'And finally' story: the quirkiest, most surprising or funniest item, which pays off the hook if the hook teased it")
    outro: str = Field(description="Max 22 words: one line teasing what to watch for in the next edition, then the signature sign-off exactly as given")
    captions: Captions
    hashtags: list[str] = Field(description="Relevant hashtags without the # sign, mix of broad and story-specific")


PROMPT = """You are the writer for "{brand}", a {edition} news video report posted as a YouTube Short, Facebook Reel and Instagram Reel.
Date: {date}. Greeting: "{greeting}". Next edition: {next_edition}. Signature sign-off: "{signoff}".

Pick the {n} most important and interesting stories below (prefer stories covered by several outlets, and those marked TRENDING), plus make sure the last one is a light, quirky "And finally" story if there is one, and write:
- a spoken script that fits in {seconds} seconds total: about {words} words across hook, welcome, segments and outro. Count carefully; going over means a story gets cut,
- facts exactly as reported: never state anything that isn't in the material, and attribute contested claims ("Reuters reports..."),
- captions for each platform, and up to {max_tags} hashtags.

How to keep people watching and coming back:
- The first three seconds decide everything. The hook is the most surprising or curious thing in today's news, told in plain words. Never start with a greeting.
- Open a loop: if the hook teases the "And finally" story, don't explain it until the end.
- Every segment starts with its most interesting detail, not background. Short sentences, active verbs, numbers where they help.
- Link segments with quick, natural transitions so it flows like one story, not a list.
- End by teasing the next edition and the signature sign-off, so the ending feels like a ritual people return for.

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
        next_edition="this evening" if edition == "morning" else "tomorrow morning",
        signoff=cfg["brand"].get("signoff", "Stay curious."),
        style=wcfg.get("style", "Clear and neutral."),
        n=vcfg["stories"],
        seconds=vcfg["max_seconds"] - 8,
        words=int((vcfg["max_seconds"] - 8) * 2.2),
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
            headline=" ".join(words[:7]),
            narration=narration,
            image_query=" ".join(sorted(s.words, key=len, reverse=True)[:3]),
            sources=s.sources[:3],
        ))
    titles = "\n".join(f"• {seg.headline}" for seg in segments)
    tags = ["news", "dailynews", "breakingnews", "worldnews", "shorts"]
    first = segments[0].headline if segments else "Today's news"
    return Script(
        hook=f"{first}. Here's what you need to know.",
        hook_text=first,
        welcome=f"{greeting}, this is {brand}.",
        segments=segments,
        outro=cfg["brand"].get("signoff", "Follow for your next update."),
        captions=Captions(
            youtube_title=f"{brand} {edition.title()} | {date_str}",
            youtube_description=f"Today's top stories:\n{titles}",
            facebook=f"{brand}, {date_str}\n{titles}",
            instagram=f"Your {edition} news in 60 seconds\n{titles}\nFollow for daily updates.",
            first_comment_question="Which of these stories matters most to you?",
        ),
        hashtags=tags,
    )
