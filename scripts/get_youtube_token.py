"""One-time helper: sign in to YouTube and print the refresh token for YOUTUBE_REFRESH_TOKEN.

1. In Google Cloud Console, enable "YouTube Data API v3".
2. Create an OAuth client ID of type "Desktop app" and download it as client_secret.json.
3. Run:  python scripts/get_youtube_token.py client_secret.json
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "client_secret.json"
    flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    info = json.load(open(path))
    client = info.get("installed") or info.get("web")
    print("YOUTUBE_CLIENT_ID=" + client["client_id"])
    print("YOUTUBE_CLIENT_SECRET=" + client["client_secret"])
    print("YOUTUBE_REFRESH_TOKEN=" + creds.refresh_token)
