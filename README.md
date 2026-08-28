# 🎤 Karaoke Maker

Paste a YouTube link, get back three MP3s: a **karaoke track** (instrumental, vocals removed), the **isolated vocals**, and the **original song** — all encoded at 320 kbps.

Vocal separation is done locally with [Demucs](https://github.com/facebookresearch/demucs) (`htdemucs` model). Nothing is sent to any external service except the YouTube download itself.

> ✂️ **Looking for the Cut & Mix studio?** It's now its own app — [**Music Mixer**](https://musicmixerweb.vercel.app/) ([source](https://github.com/budhap-dev/music-mixer)), browser-only (nothing uploaded), built in React. Make karaoke tracks here, then open them there to cut, layer, speed/pitch-shift and EQ.

## How it works

```
YouTube URL
    │  yt-dlp (best audio)
    ▼
source audio (webm/m4a)
    │  demucs --two-stems vocals
    ▼
no_vocals.wav + vocals.wav
    │  ffmpeg (libmp3lame, 320k)
    ▼
karaoke.mp3 + vocals.mp3 + original.mp3
```

Jobs run one at a time in a background worker (Demucs is CPU/GPU heavy); additional requests wait in a queue. Results are stored under `data/jobs/<job-id>/` and intermediate files are cleaned up automatically.

## Requirements

- **Python 3.10+**
- **ffmpeg** on your PATH — on macOS: `brew install ffmpeg`

## Setup (one time)

```bash
cd karaoke-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The first job you run will also download the Demucs model weights (~80 MB, cached afterwards).

## Run the app

```bash
.venv/bin/uvicorn backend.app:app
```

Then open **http://127.0.0.1:8000** in your browser:

1. Paste a YouTube link and click **Make karaoke**. Optionally tick **Keep backing/chorus vocals** to also get a karaoke variant that keeps harmonies (see note below).
2. Watch the progress — downloading → separating (takes a few minutes) → encoding.
3. Play all three tracks in the browser or download them (file sizes shown on the links).

For development, add `--reload` so code changes are picked up automatically.

## API

The web UI uses a small JSON API you can also call directly:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Start a job. Body: `{"url": "https://...", "keep_chorus": false}`. Returns `{"id": "..."}` |
| `GET` | `/api/jobs/{id}` | Job status: `stage` (`queued` / `downloading` / `separating` / `encoding` / `done` / `error`), `progress` (0–1 or null), `title`, `error`, and `sizes` (bytes per track, when done) |
| `GET` | `/api/jobs/{id}/karaoke.mp3` | Instrumental track |
| `GET` | `/api/jobs/{id}/vocals.mp3` | Vocals-only track |
| `GET` | `/api/jobs/{id}/original.mp3` | Original song |
| `GET` | `/api/jobs/{id}/karaoke_chorus.mp3` | Karaoke keeping backing vocals (only when requested) |

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=..."}'
# → {"id": "a1b2c3d4e5f6"}

curl http://127.0.0.1:8000/api/jobs/a1b2c3d4e5f6
# → {"stage": "separating", "progress": null, ...}
```

## Project layout

```
backend/
  app.py       FastAPI server: job queue, status API, file serving
  worker.py    Pipeline: yt-dlp download → demucs separation → ffmpeg encode
frontend/
  index.html   Single-page UI (no build step)
data/jobs/     Output MP3s, one folder per job (gitignored)
```

## Notes & troubleshooting

- **Job status is in memory** — restarting the server forgets past jobs; the MP3 files stay on disk under `data/jobs/` and their direct URLs keep working.
- **Old jobs are never auto-deleted** — clear out `data/jobs/` occasionally if disk space matters.
- **A job failed?** The `error` field in the job status includes the last lines of the failing tool's output.
- **`HTTP Error 403: Forbidden` (or other YouTube download errors)?** YouTube changes its site frequently and yt-dlp must keep up — upgrade it and restart the server: `.venv/bin/pip install -U yt-dlp`. Expect to do this every few weeks.
- **`address already in use` on startup?** Another process (often a previous server instance) is holding port 8000. Free it with `kill $(lsof -ti :8000)`, or run on a different port: `.venv/bin/uvicorn backend.app:app --port 8001`.
- **"Keep backing/chorus vocals"** runs a second, dedicated separation model (UVR MDX-Net Karaoke via [audio-separator](https://github.com/nomadkaraoke/python-audio-separator)) that removes *only the lead vocal*, keeping harmonies and choir. It adds ~30 s per song and downloads its model (~50 MB, cached in `~/.cache/audio-separator`) on first use. Compare it with the plain karaoke track and use whichever sounds better.
- **Separation quality**: `htdemucs` is good but not perfect — expect slight vocal bleed on dense mixes. The quality ceiling is the YouTube source audio (~130–160 kbps Opus), not the 320 kbps MP3 encode.
