# 🎤 Karaoke Maker

Paste a YouTube link, get back three MP3s: a **karaoke track** (instrumental, vocals removed), the **isolated vocals**, and the **original song** — all encoded at 320 kbps.

Then **cut & mix** on a drag-and-drop timeline: **upload your MP3s** — each lands as a clip — drag blocks to place them in time, drag their edges to cut, with times shown live. Overlap clips to blend them, or chain them into a medley, and download the result as a single MP3. (The API can additionally mix processed karaoke/vocals tracks.)

Vocal separation is done locally with [Demucs](https://github.com/facebookresearch/demucs) (`htdemucs` model). Nothing is sent to any external service except the YouTube download itself.

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

1. Paste a YouTube link and click **Make karaoke**.
2. Watch the progress — downloading → separating (takes a few minutes) → encoding.
3. Play all three tracks in the browser or download them (file sizes shown on the links).

For development, add `--reload` so code changes are picked up automatically.

## API

The web UI uses a small JSON API you can also call directly:

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Start a job. Body: `{"url": "https://..."}`. Returns `{"id": "..."}` |
| `GET` | `/api/jobs/{id}` | Job status: `stage` (`queued` / `downloading` / `separating` / `encoding` / `mixing` / `done` / `error`), `progress` (0–1 or null), `title`, `error`, and `sizes` (bytes per track, when done) |
| `GET` | `/api/jobs/{id}/karaoke.mp3` | Instrumental track |
| `GET` | `/api/jobs/{id}/vocals.mp3` | Vocals-only track |
| `GET` | `/api/jobs/{id}/original.mp3` | Original song |
| `GET` | `/api/jobs/{id}/mix.mp3` | Result of a mix job |
| `GET` | `/api/library` | All finished tracks on disk (survives restarts): `[{job, title, tracks: [{track, duration}]}]` |
| `POST` | `/api/uploads` | Add your own audio file to the library (multipart `file`). Transcoded to mp3; returns `{id, title}` |
| `POST` | `/api/mixes` | Create a mix. Body: `{"clips": [{"job", "track", "start?", "end?", "offset?", "gain?"}], "title?", "eq?": {"bass", "mid", "treble"}, "enhance?": bool}` — times in seconds; `start`/`end` cut the source, `offset` places the clip on the timeline, overlapping clips blend. `eq` is a master 3-band EQ in dB (±12); `enhance` applies the clarity chain (rumble cut, presence lift, loudness normalization). Returns `{"id"}`; poll like a normal job |

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

- **Job status is in memory** — restarting the server forgets in-flight jobs, but finished tracks stay on disk and remain usable via `/api/library` and the Cut & Mix section.
- **Old jobs are never auto-deleted** — clear out `data/jobs/` occasionally if disk space matters.
- **A job failed?** The `error` field in the job status includes the last lines of the failing tool's output.
- **`address already in use` on startup?** Another process (often a previous server instance) is holding port 8000. Free it with `kill $(lsof -ti :8000)`, or run on a different port: `.venv/bin/uvicorn backend.app:app --port 8001`.
- **Separation quality**: `htdemucs` is good but not perfect — expect slight vocal bleed on dense mixes. The quality ceiling is the YouTube source audio (~130–160 kbps Opus), not the 320 kbps MP3 encode.
