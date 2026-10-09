"""Send finished videos to a Telegram chat through a Telegram bot, ready to post by hand.

Setup: create a bot with @BotFather (gives TELEGRAM_BOT_TOKEN), send it any message, then get your
chat id from @userinfobot (TELEGRAM_CHAT_ID). Bots can upload files up to 50 MB.
"""
import logging
from pathlib import Path

import requests

from ..config import env
from . import PostResult

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_UPLOAD = 50 * 1024 * 1024
CAPTION_LIMIT = 1024   # media captions; plain messages allow 4096
MESSAGE_LIMIT = 4096


def configured() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))


def _call(method: str, **kwargs) -> dict:
    url = API.format(token=env("TELEGRAM_BOT_TOKEN"), method=method)
    r = requests.post(url, timeout=300, **kwargs)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "description": r.text[:300]}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description', r.status_code)}")
    return body["result"]


def send_text(text: str) -> None:
    for i in range(0, len(text), MESSAGE_LIMIT):
        _call("sendMessage", data={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text[i:i + MESSAGE_LIMIT],
                                   "disable_web_page_preview": True})


def send_video(video: Path, title: str, caption: str = "") -> PostResult:
    """Send the video with a short title, then the full caption as its own message so it's easy to copy."""
    size = video.stat().st_size
    if size > MAX_UPLOAD:
        return PostResult("telegram", False, error=f"{video.name} is {size / 1e6:.0f} MB; Telegram bots can send up to 50 MB")
    try:
        with open(video, "rb") as f:
            msg = _call("sendVideo", data={"chat_id": env("TELEGRAM_CHAT_ID"), "caption": title[:CAPTION_LIMIT],
                                           "supports_streaming": True},
                        files={"video": (video.name, f, "video/mp4")})
        if caption:
            send_text(caption)
    except (requests.RequestException, RuntimeError, OSError) as e:
        log.warning("Telegram: %s", e)
        return PostResult("telegram", False, error=str(e))
    return PostResult("telegram", True, post_id=str(msg.get("message_id", "")))
