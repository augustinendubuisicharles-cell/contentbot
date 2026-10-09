"""Text-to-speech for each script line, with a silent fallback so a run never stalls."""
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

import requests

from .config import DATA, ROOT, env

log = logging.getLogger(__name__)
WORDS_PER_SECOND = 2.6
ELEVEN = "https://api.elevenlabs.io/v1"
VOICE_SAMPLES = ROOT / "assets" / "voice"     # drop your own recording(s) here
VOICE_CACHE = DATA / "voice.json"
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".webm", ".mp4"}


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


async def _edge(text: str, voice: str, dest: Path) -> list[dict]:
    """Synthesize with edge-tts and return per-word timings (seconds) for captions."""
    import edge_tts
    words = []
    with open(dest, "wb") as f:
        async for chunk in edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary").stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"word": chunk["text"], "start": start, "end": start + chunk["duration"] / 1e7})
    return words


def timings_path(audio: Path) -> Path:
    return audio.with_suffix(".words.json")


def word_timings(audio: Path, text: str) -> list[dict]:
    """Per-word timings for `audio`: exact from edge-tts, otherwise spread evenly over the clip."""
    path = timings_path(audio)
    if path.exists():
        words = json.loads(path.read_text())
        if words:
            return words
    tokens = text.split()
    if not tokens:
        return []
    total = duration(audio)
    weights = [len(t) + 2 for t in tokens]
    scale, t, out = total / sum(weights), 0.0, []
    for tok, w in zip(tokens, weights):
        out.append({"word": tok, "start": t, "end": t + w * scale})
        t += w * scale
    return out


def _clone_voice(key: str) -> str | None:
    """Create an ElevenLabs voice from the recordings in assets/voice/ (once), caching its id."""
    if VOICE_CACHE.exists():
        return json.loads(VOICE_CACHE.read_text()).get("voice_id")
    samples = sorted(p for p in VOICE_SAMPLES.glob("*") if p.suffix.lower() in AUDIO_EXT) if VOICE_SAMPLES.exists() else []
    if not samples:
        return None
    files = []
    for i, sample in enumerate(samples[:5]):
        mp3 = Path("/tmp") / f"voice_sample_{i}.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(sample), "-ac", "1", "-b:a", "128k", str(mp3)],
                       check=True)
        files.append(("files", (mp3.name, mp3.read_bytes(), "audio/mpeg")))
    resp = requests.post(f"{ELEVEN}/voices/add", headers={"xi-api-key": key},
                         data={"name": "ContentBot narrator", "remove_background_noise": "true"},
                         files=files, timeout=120)
    resp.raise_for_status()
    voice_id = resp.json()["voice_id"]
    VOICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VOICE_CACHE.write_text(json.dumps({"voice_id": voice_id, "samples": [p.name for p in samples[:5]]}))
    log.info("Created ElevenLabs voice %s from %d recording(s)", voice_id, len(files))
    return voice_id


_eleven_voice: str | None = None


def _elevenlabs(text: str, dest: Path) -> bool:
    global _eleven_voice
    key = env("ELEVENLABS_API_KEY")
    if not key:
        return False
    try:
        _eleven_voice = _eleven_voice or env("ELEVENLABS_VOICE_ID") or _clone_voice(key)
        if not _eleven_voice:
            return False
        resp = requests.post(
            f"{ELEVEN}/text-to-speech/{_eleven_voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": key},
            json={"text": text, "model_id": "eleven_multilingual_v2"},
            timeout=120,
        )
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except (requests.RequestException, KeyError, subprocess.CalledProcessError) as e:
        log.warning("ElevenLabs failed (%s); falling back to the built-in voice", e)
        return False


def speak(text: str, voice: str, dest: Path) -> Path:
    """Write narration audio for `text` to `dest` (.mp3) and return the path.

    Uses your own ElevenLabs voice when ELEVENLABS_API_KEY is set, otherwise edge-tts.
    """
    if _elevenlabs(text, dest):
        return dest
    timings_path(dest).unlink(missing_ok=True)
    try:
        words = asyncio.run(_edge(text, voice, dest))
        if dest.exists() and dest.stat().st_size > 0:
            timings_path(dest).write_text(json.dumps(words))
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
