"""Clipping campaigns: turn a brand's long video into short vertical clips for pay-per-view campaigns.

    python -m contentbot.clips --url <campaign video link> --brief brief.txt --name acme
    python -m contentbot.clips --url ./podcast.mp4 --brief "Clip the funniest moments" --count 3

Steps: download the campaign's video, transcribe it with word timings (faster-whisper, free, runs on CPU),
ask Claude for the best self-contained moments that fit the brief, then cut each one to 9:16 with
burned-in captions, an on-screen hook and the ad disclosure. Writes clipNN.mp4, clips.json and post.md
(the caption to paste for each clip) to out/clips/<date>-<name>/.

Posting and submitting the links to Whop or Vyro stay with you: their sites have no submission API
for clippers, and only use footage the campaign gives you permission to clip.
"""
import argparse
import json
import logging
import re
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .config import OUT, env, load_config, load_dotenv
from .video import _ass_time, _run

log = logging.getLogger("contentbot.clips")


class Moment(BaseModel):
    start: float = Field(description="Start time in seconds, at the beginning of a sentence from the transcript")
    end: float = Field(description="End time in seconds, at the end of a sentence, once the thought or punchline lands")
    hook_text: str = Field(description="On-screen hook shown for the whole clip, max 8 words, accurate to what is said, no clickbait")
    caption: str = Field(description="Post caption: one scroll-stopping line plus anything the brief requires (mentions, links, wording). No hashtags here")
    hashtags: list[str] = Field(description="3-6 hashtags without the # sign, including any the brief requires")
    why: str = Field(description="One sentence: why this moment will hold attention")


class Moments(BaseModel):
    moments: list[Moment] = Field(description="Best first")


PROMPT = """You are an expert short-form video clipper working on a paid clipping campaign.
Find the {count} best moments in the transcript below to post as vertical clips on TikTok, YouTube Shorts and Instagram Reels.

Campaign brief (follow its rules exactly; if it bans topics or wording, avoid them):
<brief>
{brief}
</brief>

Each clip must:
- run between {min_s} and {max_s} seconds,
- make sense on its own to someone who has never seen the source,
- grab attention in the first two seconds: start on a bold claim, a surprising fact, a question, or the setup of a story, never on filler like "so", "yeah" or "um",
- end once the point, answer or punchline lands, not mid-sentence,
- not overlap with another clip.
Prefer strong emotion, conflict, humour, surprising numbers, useful advice and quotable lines. Only pick moments the brief would approve of.

Transcript, one line per sentence as [start-end seconds] text:
{transcript}"""


def download(url: str, dest_dir: Path) -> Path:
    """Fetch the campaign's video. Accepts a local path or any link yt-dlp supports (Google Drive, Dropbox, direct mp4...)."""
    local = Path(url).expanduser()
    if local.exists():
        return local
    import yt_dlp

    opts = {
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if not path.exists():  # merged files change extension
        path = next(dest_dir.glob("source.*"))
    log.info("Downloaded %s (%s)", path.name, info.get("title", ""))
    return path


def transcribe(video: Path, model_size: str, max_minutes: float, dest: Path) -> list[dict]:
    """Return sentences as [{start, end, text, words: [{word, start, end}]}], cached next to the clips."""
    cache = dest / "transcript.json"
    if cache.exists():
        return json.loads(cache.read_text())
    import numpy as np
    from faster_whisper import WhisperModel

    audio = dest / "audio.wav"
    _run(["ffmpeg", "-y", "-i", str(video), "-t", str(int(max_minutes * 60)), "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", str(audio)])
    # Decode the WAV ourselves: faster-whisper's own decoder breaks with some PyAV versions.
    with wave.open(str(audio)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(samples, word_timestamps=True, vad_filter=True)
    log.info("Transcribing %.0f minutes of %s audio with whisper-%s", info.duration / 60, info.language, model_size)
    out = []
    for seg in segments:
        words = [{"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2)}
                 for w in (seg.words or []) if w.word.strip()]
        if words:
            out.append({"start": words[0]["start"], "end": words[-1]["end"], "text": seg.text.strip(), "words": words})
    audio.unlink(missing_ok=True)
    cache.write_text(json.dumps(out))
    return out


def pick_moments(sentences: list[dict], brief: str, count: int, ccfg: dict, cfg: dict) -> list[Moment]:
    if not (env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit("Picking moments needs ANTHROPIC_API_KEY")
    transcript = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in sentences)
    prompt = PROMPT.format(count=count, brief=brief.strip() or "No special rules.",
                           min_s=ccfg["min_seconds"], max_s=ccfg["max_seconds"], transcript=transcript)
    workspace = env("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
    response = client.messages.parse(
        model=cfg["writer"]["model"],
        max_tokens=16000,
        output_config={"effort": ccfg.get("effort", "medium")},
        messages=[{"role": "user", "content": prompt}],
        output_format=Moments,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise SystemExit(f"Claude returned no moments (stop_reason={response.stop_reason})")
    return response.parsed_output.moments[:count]


def snap(moment: Moment, words: list[dict], min_s: float, max_s: float) -> tuple[float, float, list[dict]]:
    """Move the cut points onto word boundaries and keep the clip within the length limits."""
    if not words:
        return moment.start, moment.end, []
    first = min(range(len(words)), key=lambda i: abs(words[i]["start"] - moment.start))
    last = min(range(first, len(words)), key=lambda i: abs(words[i]["end"] - moment.end))
    start = words[first]["start"]
    while last > first and words[last]["end"] - start > max_s:
        last -= 1
    while last + 1 < len(words) and words[last]["end"] - start < min_s:
        last += 1
    start = max(start - 0.15, 0.0)
    end = words[last]["end"] + 0.35
    return start, end, words[first:last + 1]


def _ass_text(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(words: list[dict], start: float, length: float, hook: str, label: str, cfg: dict, dest: Path) -> Path:
    """Hook at the top, the ad label in the corner, and word-timed captions in the lower third."""
    W, H = cfg["video"]["width"], cfg["video"]["height"]
    accent = cfg["brand"]["accent_color"].lstrip("#")
    ass_accent = f"&H00{accent[4:6]}{accent[2:4]}{accent[0:2]}"  # ASS colours are BGR
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{int(W * 0.075)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,8,2,5,60,60,0,1
Style: Hook,DejaVu Sans,{int(W * 0.06)},&H00FFFFFF,&H00FFFFFF,{ass_accent},{ass_accent},-1,0,0,0,100,100,0,0,3,18,0,8,80,80,{int(H * 0.13)},1
Style: Label,DejaVu Sans,{int(W * 0.03)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,10,0,9,50,50,{int(H * 0.05)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    whole = f"{_ass_time(0)},{_ass_time(length)}"
    lines = [f"Dialogue: 1,{whole},Hook,,0,0,0,,{_ass_text(hook).upper()}"]
    if label:
        lines.append(f"Dialogue: 1,{whole},Label,,0,0,0,,{_ass_text(label)}")
    y = int(H * 0.74)
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    for i, chunk in enumerate(chunks):
        a = max(chunk[0]["start"] - start, 0)
        b = (chunks[i + 1][0]["start"] if i + 1 < len(chunks) else chunk[-1]["end"] + 0.3) - start
        text = _ass_text(" ".join(w["word"] for w in chunk)).upper()
        anim = r"{\pos(%d,%d)\fscx112\fscy112\t(0,120,\fscx100\fscy100)}" % (W // 2, y)
        lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(min(b, length))},Cap,,0,0,0,,{anim}{text}")
    dest.write_text(header + "\n".join(lines) + "\n")
    return dest


def render_clip(src: Path, start: float, end: float, subs: Path, layout: str, cfg: dict, dest: Path) -> Path:
    W, H, fps = cfg["video"]["width"], cfg["video"]["height"], cfg["video"]["fps"]
    if layout == "crop":
        # Fill the screen with the middle of the frame: best for one person talking to camera.
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[base]"
    else:
        # Whole frame in the middle over a blurred copy: nothing important gets cut off.
        vf = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=24:4,eq=brightness=-0.12[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    vf += f";[base]ass='{subs.resolve()}',fps={fps},format=yuv420p[v]"
    _run([
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{end - start:.2f}",
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart", str(dest),
    ])
    return dest


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "campaign"


def make_clips(url: str, brief: str, name: str, count: int, layout: str, cfg: dict) -> Path:
    ccfg = cfg["clips"]
    run_dir = OUT / "clips" / f"{datetime.now(timezone.utc):%Y-%m-%d}-{slugify(name)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    src = download(url, run_dir)
    sentences = transcribe(src, ccfg["whisper_model"], ccfg["max_source_minutes"], run_dir)
    if not sentences:
        raise SystemExit("No speech found in the video, so there is nothing to clip")
    words = [w for s in sentences for w in s["words"]]
    moments = pick_moments(sentences, brief, count, ccfg, cfg)

    label = ccfg.get("disclosure", "#ad")
    results, notes = [], [f"# Clips: {name}\n", f"Source: {url}\n"]
    for i, m in enumerate(moments, 1):
        start, end, clip_words = snap(m, words, ccfg["min_seconds"], ccfg["max_seconds"])
        subs = write_ass(clip_words, start, end - start, m.hook_text, ccfg.get("on_screen_label", label), cfg,
                         run_dir / f"clip{i:02d}.ass")
        video = render_clip(src, start, end, subs, layout, cfg, run_dir / f"clip{i:02d}.mp4")
        tags = " ".join("#" + t.lstrip("#").replace(" ", "") for t in m.hashtags)
        caption = f"{m.caption}\n\n{label} {tags}".strip()
        results.append({"file": video.name, "start": round(start, 2), "end": round(end, 2), "hook": m.hook_text,
                        "caption": caption, "why": m.why})
        notes.append(f"## {video.name} ({end - start:.0f}s, from {int(start // 60)}:{start % 60:04.1f})\n\n"
                     f"Hook: {m.hook_text}\n\nCaption to paste:\n\n```\n{caption}\n```\n\nWhy: {m.why}\n")
        log.info("Clip %d: %.1f-%.1fs %s", i, start, end, m.hook_text)

    notes.append("After posting, turn on the platform's paid-partnership / branded-content label if the brief asks for it, "
                 "then submit each post link on the campaign page.\n")
    (run_dir / "clips.json").write_text(json.dumps(results, indent=2))
    (run_dir / "post.md").write_text("\n".join(notes))
    return run_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contentbot.clips", description="Cut campaign footage into short vertical clips")
    ap.add_argument("--url", required=True, help="campaign video link or local file")
    ap.add_argument("--brief", default="", help="the campaign's rules, as text or a path to a text file")
    ap.add_argument("--name", default="", help="campaign name, used for the output folder")
    ap.add_argument("--count", type=int, default=0, help="how many clips (default from config.yaml)")
    ap.add_argument("--layout", choices=["blur", "crop"], default="", help="blur: whole frame over a blurred fill; "
                    "crop: fill the screen with the middle of the frame")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    cfg = load_config(args.config)
    brief = args.brief
    if brief and len(brief) < 300 and Path(brief).is_file():
        brief = Path(brief).read_text()
    try:
        run_dir = make_clips(args.url, brief, args.name or "campaign", args.count or cfg["clips"]["count"],
                             args.layout or cfg["clips"]["layout"], cfg)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed: %s", e.stderr.decode(errors="replace")[-2000:] if e.stderr else e)
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
