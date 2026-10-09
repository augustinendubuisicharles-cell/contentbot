"""ContentBot command line.

    python -m contentbot run --edition morning            # build and post
    python -m contentbot run --edition evening --dry-run  # build the video only
    python -m contentbot report                           # fetch stats, write data/report.md
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import growth
from .config import OUT, load_config, load_dotenv
from .gather import gather
from .images import find_image
from .publish import PostResult, meta, tiktok, youtube
from .rank import rank
from .video import Scene, compose_frame, concat, render_scene, write_captions
from .voice import duration, speak, word_timings
from .writer import write_script

log = logging.getLogger("contentbot")


def build(cfg: dict, edition: str) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    date_str = now.strftime("%a %d %b %Y")
    run_dir = OUT / f"{now:%Y-%m-%d}-{edition}"
    run_dir.mkdir(parents=True, exist_ok=True)

    items, trends = gather(cfg, cfg["editions"][edition]["lookback_hours"])
    if not items:
        raise SystemExit("No stories gathered; check network access and sources in config.yaml")
    stories = rank(items, trends)
    script = write_script(stories, cfg, edition, date_str)
    (run_dir / "script.json").write_text(script.model_dump_json(indent=2))

    vcfg = cfg["video"]
    voice = vcfg["voice"]
    used: set[str] = set()
    scenes: list[Scene] = []
    credits: list[str] = []

    def narrate(text: str, name: str) -> tuple[str, Path]:
        return text, speak(text, voice, run_dir / name, vcfg.get("kokoro_voice", ""), vcfg.get("speed", 1.0))

    # Cold open: the hook comes before any greeting.
    text, audio = narrate(f"{script.hook} {script.welcome}", "a00.mp3")
    scenes.append(Scene(script.hook_text, audio, kicker=edition.upper(), narration_text=text))
    for i, seg in enumerate(script.segments, 1):
        img = find_image(seg.image_query, run_dir / f"img{i:02d}", cfg["images"]["commercial_only"], used)
        credits.append(img.credit)
        text, audio = narrate(seg.narration, f"a{i:02d}.mp3")
        scenes.append(Scene(seg.headline, audio, img.path, credit=f"Sources: {', '.join(seg.sources[:3])}",
                            narration_text=text))
    text, audio = narrate(script.outro, "a99.mp3")
    scenes.append(Scene(cfg["brand"].get("signoff", "See you next time"), audio,
                        kicker=f"FOLLOW {cfg['brand']['handle']}".upper(), narration_text=text))

    # Fit inside the time limit. Keep the "And finally" payoff: drop the story before it.
    limit = cfg["video"]["max_seconds"]
    durations = [duration(s.narration_audio) + 0.35 for s in scenes]
    while sum(durations) > limit and len(scenes) > 4:
        log.info("Report is %.1fs; dropping a story to fit %ds", sum(durations), limit)
        del scenes[-3], durations[-3]

    stories_in = scenes[1:-1]
    for i, scene in enumerate(stories_in, 1):
        scene.kicker = "AND FINALLY" if i == len(stories_in) and i > 1 else f"{i} OF {len(stories_in)}"

    clips = []
    for i, (scene, secs) in enumerate(zip(scenes, durations)):
        frame = compose_frame(scene, cfg, date_str, run_dir / f"f{i:02d}.jpg")
        caps = write_captions(word_timings(scene.narration_audio, scene.narration_text), cfg,
                              run_dir / f"s{i:02d}.ass")
        clips.append(render_scene(frame, scene.narration_audio, secs, cfg, run_dir / f"c{i:02d}.mp4", caps))
    video = concat(clips, run_dir / "report.mp4", cfg, cfg["video"].get("background_music", ""))
    log.info("Rendered %s (%.1fs)", video, duration(video))

    all_sources = sorted({s for seg in script.segments for s in seg.sources})
    return {"video": video, "script": script, "credits": credits, "sources": all_sources,
            "run_dir": run_dir, "edition": edition}


def publish(cfg: dict, built: dict, only: set[str] | None) -> list[PostResult]:
    script, video = built["script"], built["video"]
    tags = growth.hashtags(script.hashtags, cfg["growth"]["max_hashtags"])
    credits = growth.credits_block(built["credits"])
    comment = growth.first_comment(script.captions.first_comment_question, built["sources"])
    wanted = {p for p, on in cfg["platforms"].items() if on and (not only or p in only)}
    results: list[PostResult] = []

    if "youtube" in wanted:
        if youtube.configured():
            desc = f"{script.captions.youtube_description}\n\nSources: {', '.join(built['sources'])}\n\n{credits}\n\n{growth.tag_line(tags)}"
            results.append(youtube.upload(video, script.captions.youtube_title, desc, tags))
        else:
            log.warning("YouTube credentials missing; skipping")
    if "facebook" in wanted:
        if meta.facebook_configured():
            r = meta.post_facebook_reel(video, f"{script.captions.facebook}\n\n{growth.tag_line(tags)}\n\n{credits}")
            if r.ok and cfg["growth"]["first_comment"]:
                meta.comment(r.post_id, comment)
            results.append(r)
        else:
            log.warning("Facebook credentials missing; skipping")
    if "instagram" in wanted:
        if meta.instagram_configured():
            r = meta.post_instagram_reel(video, f"{script.captions.instagram}\n\n{growth.tag_line(tags)}")
            if r.ok and cfg["growth"]["first_comment"]:
                meta.comment(r.post_id, f"{comment}\n\n{credits}"[:2200])
            results.append(r)
        else:
            log.warning("Instagram credentials missing; skipping")

    if "tiktok" in wanted:
        if tiktok.configured():
            caption = f"{script.captions.instagram}\n\n{growth.tag_line(tags)}"
            results.append(tiktok.post(video, caption, cfg.get("tiktok", {}).get("mode", "draft")))
        else:
            log.warning("TikTok credentials missing; skipping")

    for r in results:
        growth.log_post({
            "posted_at": datetime.now(timezone.utc).isoformat(), "edition": built["edition"],
            "platform": r.platform, "ok": r.ok, "post_id": r.post_id, "url": r.url,
            "title": script.captions.youtube_title, "error": r.error,
        })
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contentbot")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build the report video and post it")
    run.add_argument("--edition", choices=["morning", "evening", "auto"], default="auto")
    run.add_argument("--dry-run", action="store_true", help="build the video but don't post")
    run.add_argument("--platforms", default="", help="comma list to limit posting, e.g. youtube,instagram")
    sub.add_parser("report", help="fetch post stats and write data/report.md")
    sub.add_parser("tiktok-url", help="print the TikTok sign-in link")
    ta = sub.add_parser("tiktok-auth", help="connect TikTok with the code from the sign-in page")
    ta.add_argument("--code", required=True)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(args.config)

    redirect = cfg.get("tiktok", {}).get("redirect_uri", "")
    if args.cmd == "tiktok-url":
        print(tiktok.auth_url(redirect))
        return 0
    if args.cmd == "tiktok-auth":
        from urllib.parse import unquote
        tiktok.exchange_code(unquote(args.code), redirect)
        return 0
    if args.cmd == "report":
        print(growth.report(cfg["timezone"]).read_text())
        return 0

    edition = args.edition
    if edition == "auto":
        edition = "morning" if datetime.now(ZoneInfo(cfg["timezone"])).hour < 14 else "evening"
    built = build(cfg, edition)
    if args.dry_run:
        sc = built["script"]
        print(json.dumps({"video": str(built["video"]), "title": sc.captions.youtube_title,
                          "narration": [f"{sc.hook} {sc.welcome}", *[seg.narration for seg in sc.segments], sc.outro]}, indent=2))
        return 0
    only = {p.strip() for p in args.platforms.split(",") if p.strip()} or None
    results = publish(cfg, built, only)
    for r in results:
        print(f"{r.platform}: {'posted ' + r.url if r.ok else 'FAILED ' + r.error}")
    if not results:
        log.warning("No platform credentials set; the video was built but not posted")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
