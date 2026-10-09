"""Post to TikTok with the Content Posting API.

Until TikTok audits your developer app it only allows private posts, so the
default mode is "draft": the video lands in your TikTok inbox and you publish it
with one tap. After the audit, set platforms_settings.tiktok.mode to "direct".

TikTok may rotate the refresh token whenever it is used, so the newest one is
kept in data/.tiktok_token.json (restored between runs by the Actions cache,
never committed) and the TIKTOK_REFRESH_TOKEN secret is only the starting point.
"""
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..config import DATA, env
from . import PostResult

log = logging.getLogger(__name__)
API = "https://open.tiktokapis.com/v2"
TOKEN_FILE = DATA / ".tiktok_token.json"
SCOPES = "user.info.basic,video.upload,video.publish"


def configured() -> bool:
    return bool(env("TIKTOK_CLIENT_KEY") and env("TIKTOK_CLIENT_SECRET")
                and (TOKEN_FILE.exists() or env("TIKTOK_REFRESH_TOKEN")))


def _check(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    err = data.get("error")
    if resp.status_code >= 400 or (isinstance(err, dict) and err.get("code") not in (None, "ok")):
        msg = err.get("message") if isinstance(err, dict) else data.get("error_description", resp.text)
        code = err.get("code") if isinstance(err, dict) else err
        raise RuntimeError(f"{code}: {msg}")
    return data


def _save(token: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"refresh_token": token["refresh_token"], "saved_at": int(time.time())}))


def auth_url(redirect_uri: str, state: str = "contentbot") -> str:
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": env("TIKTOK_CLIENT_KEY"), "scope": SCOPES, "response_type": "code",
        "redirect_uri": redirect_uri, "state": state,
    })


def exchange_code(code: str, redirect_uri: str) -> None:
    """One-time: swap the code from TikTok's sign-in page for a refresh token."""
    token = _check(requests.post(f"{API}/oauth/token/", data={
        "client_key": env("TIKTOK_CLIENT_KEY"), "client_secret": env("TIKTOK_CLIENT_SECRET"),
        "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }, timeout=30))
    _save(token)
    log.info("TikTok connected (scopes: %s)", token.get("scope"))


def _access_token() -> str:
    refresh = json.loads(TOKEN_FILE.read_text())["refresh_token"] if TOKEN_FILE.exists() else env("TIKTOK_REFRESH_TOKEN")
    token = _check(requests.post(f"{API}/oauth/token/", data={
        "client_key": env("TIKTOK_CLIENT_KEY"), "client_secret": env("TIKTOK_CLIENT_SECRET"),
        "grant_type": "refresh_token", "refresh_token": refresh,
    }, timeout=30))
    _save(token)
    return token["access_token"]


def _upload(upload_url: str, video: Path) -> None:
    size = video.stat().st_size
    with open(video, "rb") as f:
        resp = requests.put(upload_url, data=f, timeout=600, headers={
            "Content-Type": "video/mp4", "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        })
    resp.raise_for_status()


def post(video: Path, caption: str, mode: str = "draft") -> PostResult:
    size = video.stat().st_size
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    try:
        headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json; charset=UTF-8"}
        if mode == "direct":
            creator = _check(requests.post(f"{API}/post/publish/creator_info/query/", headers=headers, timeout=30))
            options = creator["data"].get("privacy_level_options", [])
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else (options or ["SELF_ONLY"])[0]
            body = {"post_info": {"title": caption[:2200], "privacy_level": privacy, "is_aigc": True,
                                  "brand_content_toggle": False}, "source_info": source}
            init = _check(requests.post(f"{API}/post/publish/video/init/", headers=headers, json=body, timeout=60))
        else:
            init = _check(requests.post(f"{API}/post/publish/inbox/video/init/", headers=headers,
                                        json={"source_info": source}, timeout=60))
        publish_id = init["data"]["publish_id"]
        _upload(init["data"]["upload_url"], video)
        status = ""
        for _ in range(30):
            res = _check(requests.post(f"{API}/post/publish/status/fetch/", headers=headers,
                                       json={"publish_id": publish_id}, timeout=30))
            status = res["data"].get("status", "")
            if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                break
            if status == "FAILED":
                raise RuntimeError(res["data"].get("fail_reason", "publish failed"))
            time.sleep(10)
        note = "in your TikTok inbox, tap to publish" if status == "SEND_TO_USER_INBOX" else status.lower()
        log.info("TikTok: %s", note)
        return PostResult("tiktok", True, publish_id, "")
    except (requests.RequestException, RuntimeError, KeyError) as e:
        log.error("TikTok post failed: %s", e)
        return PostResult("tiktok", False, error=str(e))
