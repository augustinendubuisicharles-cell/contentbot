# ContentBot

Twice a day, ContentBot reads the news, picks the biggest stories, writes a one-minute script,
narrates it over openly licensed photos, and posts the video as a YouTube Short, a Facebook Reel
and an Instagram Reel.

```
gather  →  rank  →  write  →  images + voice  →  video  →  post  →  track
RSS, Reddit,  group same   Claude writes   Openverse /         ffmpeg   YouTube,   views per post,
X, Google     story, score script, captions Wikimedia CC        9:16     Facebook,  best posting
Trends        by coverage  and hashtags     photos, edge-tts    60s      Instagram  hours
```

## Run it locally

```bash
pip install -r requirements.txt        # also needs ffmpeg on PATH
cp .env.example .env                   # fill in what you have; everything is optional for a dry run
python -m contentbot run --edition morning --dry-run
```

The video lands in `out/<date>-<edition>/report.mp4` with `script.json` next to it.
Without an Anthropic key it uses a plain headline script; without platform keys it skips posting.

## Schedule (GitHub Actions)

`.github/workflows/post.yml` runs the morning and evening editions and a weekly performance report.
Cron times are **UTC**: change the two `cron:` lines (and the matching strings in "Pick edition")
to your local morning and evening. Run it by hand from the Actions tab with "Run workflow".

Add these under Settings → Secrets and variables → Actions:

| Secret | Where it comes from |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | Google Cloud: enable YouTube Data API v3, make a Desktop OAuth client, run `python scripts/get_youtube_token.py client_secret.json` |
| `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN`, `META_IG_USER_ID` | developers.facebook.com: create an app, add the Pages and Instagram products, get a long-lived Page token with `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`, `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`. The IG account must be a Business or Creator account linked to the Page. |
| `X_BEARER_TOKEN` (optional) | developer.x.com, Basic tier or above (free tier can't search) |

Notes on the platforms:
- YouTube: new API projects upload as **private** until the project passes Google's audit. Request it in Google Cloud once you've tested.
- Meta: for your own Page and IG account, Development mode is enough. Long-lived Page tokens don't expire unless you change your password or remove the app.

## Growing the audience (the legitimate way)

Built in:
- **Trend boost**: stories that match today's Google Trends searches are ranked higher, so reports ride what people are already searching for.
- **Platform-specific captions and hashtags** written per edition by Claude.
- **First comment** on Facebook and Instagram with the sources and a discussion question, which invites real replies.
- **Credits and sources** in every post, which keeps you within Creative Commons terms and builds trust.
- **Performance report** (`python -m contentbot report`, also weekly in Actions): views per platform, edition and hour posted, written to `data/report.md`, so you can move the schedule to the hours that work.

Not built, on purpose: fake accounts, bought or bot likes, views or follows, and automated comments
on other people's posts. YouTube and Meta detect these and restrict or ban accounts, which would undo all of this.

## TikTok

1. At [developers.tiktok.com](https://developers.tiktok.com) create an app, add **Login Kit** and **Content Posting API**,
   and set the redirect URI, privacy policy and terms URLs to this repo's GitHub Pages site
   (`https://<user>.github.io/contentbot/`, `.../privacy.html`, `.../terms.html`).
2. Add `TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` as secrets.
3. Run `python -m contentbot tiktok-url` (or ask Claude for the link), sign in, copy the code shown on the page,
   then run the workflow with edition `tiktok-auth` and paste the code.

Until TikTok audits the app, videos arrive in your TikTok inbox as drafts (`tiktok.mode: draft`) and you tap to publish.
After the audit, set `tiktok.mode: direct`.

## Your own voice

Put a 1-2 minute recording of yourself (any common audio format) in `assets/voice/`, and add an
`ELEVENLABS_API_KEY` secret. The first run turns it into an ElevenLabs voice and saves its id in
`data/voice.json`; every video after that is narrated in your voice. To use a voice you already made
on elevenlabs.io instead, set `ELEVENLABS_VOICE_ID`. Keep the repo private if it holds your recording.

## Configuration

Everything else is in `config.yaml`: brand name and colour, time zone, sources, stories per video,
voice (any edge-tts voice, e.g. `en-GB-RyanNeural`, `en-NG-AbeoNeural`), optional background music.

## Images and copyright

Only Creative Commons and public-domain images from Openverse and Wikimedia Commons are used,
filtered to licences that allow commercial use. When nothing suitable is found, a branded title card is used instead.
News-site photos are never downloaded, since they're usually copyrighted.
