"""Find openly licensed images (Openverse, Wikimedia Commons) and record credits."""
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "ContentBot/1.0 (daily news summary; image credits included in posts)"}


@dataclass
class Image:
    path: Path | None
    credit: str          # "Title by Creator, CC BY 2.0, via Openverse" style line; empty for generated cards


def _openverse(query: str, commercial: bool) -> list[dict]:
    params = {"q": query, "page_size": 10, "mature": "false", "aspect_ratio": "tall,square,wide"}
    if commercial:
        params["license_type"] = "commercial,modification"
    resp = requests.get("https://api.openverse.org/v1/images/", params=params, headers=UA, timeout=20)
    resp.raise_for_status()
    out = []
    for r in resp.json().get("results", []):
        if (r.get("width") or 0) < 800:
            continue
        lic = f"{r.get('license', '').upper()} {r.get('license_version', '')}".strip()
        out.append({
            "url": r["url"],
            "credit": f"\"{r.get('title') or 'Photo'}\" by {r.get('creator') or 'unknown'}, {lic} ({r.get('foreign_landing_url', '')})",
        })
    return out


def _commons(query: str) -> list[dict]:
    resp = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 10,
            "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1600,
        },
        headers=UA, timeout=20,
    )
    resp.raise_for_status()
    out = []
    for page in (resp.json().get("query", {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        if (info.get("width") or 0) < 800:
            continue
        artist = requests.utils.unquote(meta.get("Artist", {}).get("value", "unknown"))
        artist = artist.split(">")[-2].split("<")[0] if "<" in artist else artist
        lic = meta.get("LicenseShortName", {}).get("value", "")
        out.append({
            "url": info.get("thumburl") or info["url"],
            "credit": f"{page['title'].removeprefix('File:')} by {artist}, {lic}, via Wikimedia Commons ({info.get('descriptionurl', '')})",
        })
    return out


def find_image(query: str, dest: Path, commercial: bool = True, used: set[str] | None = None) -> Image:
    used = used if used is not None else set()
    for search in (lambda q: _openverse(q, commercial), _commons):
        try:
            candidates = search(query)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("Image search %r failed: %s", query, e)
            continue
        for c in candidates:
            if c["url"] in used:
                continue
            try:
                img = requests.get(c["url"], headers=UA, timeout=30)
                img.raise_for_status()
                if not img.headers.get("content-type", "").startswith("image/"):
                    continue
            except requests.RequestException:
                continue
            dest.write_bytes(img.content)
            used.add(c["url"])
            return Image(dest, c["credit"])
    log.info("No licensed image for %r; using a title card", query)
    return Image(None, "")
