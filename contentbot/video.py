"""Compose vertical 9:16 frames with Pillow and render the report with ffmpeg."""
import logging
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageFont, ImageOps

log = logging.getLogger(__name__)

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


@dataclass
class Scene:
    text: str               # big on-screen text
    narration_audio: Path
    image: Path | None = None
    kicker: str = ""        # small label above the text, e.g. "1/5"
    credit: str = ""        # small source/credit line


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def compose_frame(scene: Scene, cfg: dict, date_str: str, dest: Path) -> Path:
    W, H = cfg["video"]["width"], cfg["video"]["height"]
    accent = _hex(cfg["brand"]["accent_color"])
    # Render at 1.15x so the slow zoom never reveals edges.
    w, h = int(W * 1.15), int(H * 1.15)

    if scene.image and scene.image.exists():
        src = ImageOps.exif_transpose(PILImage.open(scene.image)).convert("RGB")
        bg = ImageOps.fit(src, (w, h)).filter(ImageFilter.GaussianBlur(40))
        bg = PILImage.blend(bg, PILImage.new("RGB", (w, h), (0, 0, 0)), 0.45)
        fg = ImageOps.contain(src, (w, int(h * 0.55)))
        bg.paste(fg, ((w - fg.width) // 2, int(h * 0.18)))
        canvas = bg
    else:
        canvas = PILImage.new("RGB", (w, h), (18, 18, 24))
        grad = PILImage.linear_gradient("L").resize((w, h))
        canvas = PILImage.composite(PILImage.new("RGB", (w, h), tuple(int(v * 0.5) for v in accent)), canvas, grad)

    d = ImageDraw.Draw(canvas)
    pad = int(w * 0.11)

    # Brand bar
    bar_h = int(h * 0.075)
    d.rectangle([0, int(h * 0.085), w, int(h * 0.085) + bar_h], fill=accent)
    d.text((pad, int(h * 0.085) + bar_h // 2), cfg["brand"]["name"].upper(), font=font(int(bar_h * 0.45)),
           fill="white", anchor="lm")
    d.text((w - pad, int(h * 0.085) + bar_h // 2), date_str, font=font(int(bar_h * 0.32)), fill="white", anchor="rm")

    # Headline block in the lower third
    head_font = font(int(w * 0.068))
    lines = textwrap.wrap(scene.text, width=20)[:4]
    line_h = int(head_font.size * 1.22)
    top = int(h * 0.74) - (len(lines) * line_h) // 2
    box = [pad - 30, top - 90, w - pad + 30, top + len(lines) * line_h + 40]
    overlay = PILImage.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(box, radius=28, fill=(0, 0, 0, 175))
    canvas = PILImage.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(canvas)
    if scene.kicker:
        d.text((pad, top - 70), scene.kicker, font=font(int(w * 0.035)), fill=accent)
    for i, line in enumerate(lines):
        d.text((pad, top + i * line_h), line, font=head_font, fill="white")
    if scene.credit:
        small = font(int(w * 0.022))
        for i, line in enumerate(textwrap.wrap(scene.credit, width=60)[:2]):
            d.text((pad, int(h * 0.86) + i * int(small.size * 1.3)), line, font=small, fill=(220, 220, 220))

    canvas.save(dest, quality=92)
    return dest


def _run(cmd: list[str]) -> None:
    log.debug("ffmpeg: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True)


def render_scene(frame: Path, audio: Path, seconds: float, cfg: dict, dest: Path) -> Path:
    W, H, fps = cfg["video"]["width"], cfg["video"]["height"], cfg["video"]["fps"]
    frames = max(int(seconds * fps), 1)
    zoom = f"zoompan=z='min(zoom+0.0006,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={fps}"
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(frame), "-i", str(audio),
        "-filter_complex", f"[0:v]{zoom},format=yuv420p[v];[1:a]apad,atrim=0:{seconds:.3f},aresample=44100[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        str(dest),
    ])
    return dest


def concat(clips: list[Path], dest: Path, music: str = "") -> Path:
    listing = dest.with_suffix(".txt")
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    joined = dest.with_name(dest.stem + "_nomusic.mp4") if music else dest
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
          "-movflags", "+faststart", str(joined)])
    if music and Path(music).exists():
        _run([
            "ffmpeg", "-y", "-i", str(joined), "-stream_loop", "-1", "-i", music,
            "-filter_complex", "[1:a]volume=0.12[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(dest),
        ])
    return dest
