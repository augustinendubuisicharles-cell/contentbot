"""Upload to YouTube as a Short via the YouTube Data API v3."""
import logging
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import env
from . import PostResult

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def configured() -> bool:
    return all(env(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"))


def client():
    creds = Credentials(
        None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video: Path, title: str, description: str, tags: list[str]) -> PostResult:
    if "#shorts" not in (title + description).lower():
        description += "\n#Shorts"
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:15],
            "categoryId": "25",  # News & Politics
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    try:
        req = client().videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True),
        )
        response = None
        while response is None:
            _, response = req.next_chunk()
        vid = response["id"]
        return PostResult("youtube", True, vid, f"https://youtube.com/shorts/{vid}")
    except HttpError as e:
        log.error("YouTube upload failed: %s", e)
        return PostResult("youtube", False, error=str(e))


def stats(video_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(video_ids), 50):
        resp = client().videos().list(part="statistics", id=",".join(video_ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            s = item["statistics"]
            out[item["id"]] = {
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            }
    return out
