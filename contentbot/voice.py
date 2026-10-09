"""Text-to-speech for each script line, with a silent fallback so a run never stalls."""
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
WORDS_PER_SECOND = 2.6


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


async def _edge(text: str, voice: str, dest: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate="+8%").save(str(dest))


def speak(text: str, voice: str, dest: Path) -> Path:
    """Write narration audio for `text` to `dest` (.mp3) and return the path."""
    try:
        asyncio.run(_edge(text, voice, dest))
        if dest.exists() and dest.stat().st_size > 0:
            return dest
    except Exception as e:  # edge-tts raises a variety of network errors
        log.warning("edge-tts failed (%s)", e)
    if shutil.which("espeak-ng"):
        wav = dest.with_suffix(".wav")
        subprocess.run(["espeak-ng", "-s", "165", "-w", str(wav), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(dest)], check=True)
        return dest
    secs = max(len(text.split()) / WORDS_PER_SECOND, 2.0)
    log.warning("No TTS available; writing %.1fs of silence", secs)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", f"{secs:.2f}", str(dest)],
        check=True,
    )
    return dest
