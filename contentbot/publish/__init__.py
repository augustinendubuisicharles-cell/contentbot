from dataclasses import dataclass


@dataclass
class PostResult:
    platform: str
    ok: bool
    post_id: str = ""
    url: str = ""
    error: str = ""
