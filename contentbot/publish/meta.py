"""Post Reels to a Facebook Page and an Instagram professional account via the Meta Graph API.

Both use Meta's resumable upload, so the video file is sent directly and no
public hosting is needed.
"""
import logging
import time
from pathlib import Path

import requests

from ..config import env
from . import PostResult

log = logging.getLogger(__name__)


def _graph() -> str:
    return f"https://graph.facebook.com/{env('META_GRAPH_VERSION') or 'v23.0'}"


def _token() -> str:
    return env("META_PAGE_ACCESS_TOKEN") or ""


def _check(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise
    if resp.status_code >= 400 or "error" in data:
        raise RuntimeError(data.get("error", {}).get("message", resp.text))
    return data


def facebook_configured() -> bool:
    return bool(env("META_PAGE_ID") and _token())


def instagram_configured() -> bool:
    return bool(env("META_IG_USER_ID") and _token())


def _rupload(url: str, video: Path) -> None:
    size = video.stat().st_size
    with open(video, "rb") as f:
        resp = requests.post(
            url,
            headers={"Authorization": f"OAuth {_token()}", "offset": "0", "file_size": str(size)},
            data=f,
            timeout=600,
        )
    _check(resp)


def post_facebook_reel(video: Path, description: str) -> PostResult:
    page = env("META_PAGE_ID")
    try:
        start = _check(requests.post(f"{_graph()}/{page}/video_reels",
                                     data={"upload_phase": "start", "access_token": _token()}, timeout=60))
        video_id = start["video_id"]
        _rupload(start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{_graph().rsplit('/', 1)[1]}/{video_id}", video)
        _check(requests.post(f"{_graph()}/{page}/video_reels", data={
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": description, "access_token": _token(),
        }, timeout=60))
        return PostResult("facebook", True, video_id, f"https://www.facebook.com/reel/{video_id}")
    except (requests.RequestException, RuntimeError, KeyError) as e:
        log.error("Facebook Reel failed: %s", e)
        return PostResult("facebook", False, error=str(e))


def post_instagram_reel(video: Path, caption: str, wait_seconds: int = 600) -> PostResult:
    ig = env("META_IG_USER_ID")
    try:
        container = _check(requests.post(f"{_graph()}/{ig}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
            "share_to_feed": "true", "access_token": _token(),
        }, timeout=60))
        cid = container["id"]
        _rupload(container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{_graph().rsplit('/', 1)[1]}/{cid}", video)
        deadline = time.time() + wait_seconds
        while True:
            status = _check(requests.get(f"{_graph()}/{cid}",
                                         params={"fields": "status_code,status", "access_token": _token()}, timeout=30))
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED") or time.time() > deadline:
                raise RuntimeError(f"container {code}: {status.get('status')}")
            time.sleep(10)
        published = _check(requests.post(f"{_graph()}/{ig}/media_publish",
                                         data={"creation_id": cid, "access_token": _token()}, timeout=60))
        media_id = published["id"]
        link = _check(requests.get(f"{_graph()}/{media_id}",
                                   params={"fields": "permalink", "access_token": _token()}, timeout=30))
        return PostResult("instagram", True, media_id, link.get("permalink", ""))
    except (requests.RequestException, RuntimeError, KeyError) as e:
        log.error("Instagram Reel failed: %s", e)
        return PostResult("instagram", False, error=str(e))


def comment(object_id: str, message: str) -> None:
    """Post a first comment (sources + discussion question) on a FB video or IG media."""
    try:
        _check(requests.post(f"{_graph()}/{object_id}/comments",
                             data={"message": message, "access_token": _token()}, timeout=30))
    except (requests.RequestException, RuntimeError) as e:
        log.warning("First comment on %s failed: %s", object_id, e)


def instagram_insights(media_id: str) -> dict:
    data = _check(requests.get(f"{_graph()}/{media_id}/insights", params={
        "metric": "views,reach,likes,comments,shares,saved", "access_token": _token(),
    }, timeout=30))
    return {m["name"]: m["values"][0]["value"] for m in data.get("data", [])}


def facebook_reel_insights(video_id: str) -> dict:
    data = _check(requests.get(f"{_graph()}/{video_id}/video_insights", params={"access_token": _token()}, timeout=30))
    wanted = {"blue_reels_play_count": "views", "post_impressions_unique": "reach"}
    return {wanted[m["name"]]: m["values"][0]["value"] for m in data.get("data", []) if m["name"] in wanted}
