# Technical Design Document — Karaoke Maker

| | |
|---|---|
| **Status** | Implemented |
| **Last updated** | 2026-07-26 |
| **Repo** | `karaoke-app` |

## 1. Overview

Karaoke Maker is a self-hosted web app that turns a YouTube link into three MP3s: an instrumental **karaoke** track, an isolated **vocals** track, and the **original** song. All processing (download, ML vocal separation, encoding) runs locally on the user's machine; no third-party processing service is involved.

### Goals
- One-click flow: paste URL → get playable/downloadable tracks in the browser.
- Fully local processing for privacy and zero per-use cost.
- Live progress feedback for the long-running stages.
- Minimal footprint: no database, no build step, no background services beyond the one server process.

### Non-goals
- Multi-user or public deployment (no auth, no rate limiting, no HTTPS).
- Persistence of job history across server restarts.
- Multi-track stem separation (drums/bass/other) — only two-stem vocals/instrumental.

## 2. Architecture

```
┌──────────────┐   HTTP (JSON + mp3)   ┌─────────────────────────────────────┐
│   Browser    │ ◄───────────────────► │  FastAPI server (backend/app.py)    │
│ index.html   │   POST /api/jobs      │                                     │
│ (vanilla JS, │   GET  /api/jobs/{id} │  ┌───────────────────────────────┐  │
│  polling)    │   GET  .../{track}.mp3│  │ ThreadPoolExecutor(1 worker)  │  │
└──────────────┘                       │  │   run_pipeline (worker.py)    │  │
                                       │  │   ├─ yt-dlp   (download)      │  │
                                       │  │   ├─ demucs   (subprocess)    │  │
                                       │  │   └─ ffmpeg   (subprocess)    │  │
                                       │  └───────────────────────────────┘  │
                                       │  jobs: dict[str, dict] + Lock       │
                                       └───────────────┬─────────────────────┘
                                                       │ writes
                                                       ▼
                                              data/jobs/<job_id>/
                                              karaoke.mp3 vocals.mp3 original.mp3
```

### Components

| Component | File | Responsibility |
|---|---|---|
| Frontend | `frontend/index.html` | Submit URL, poll status, render progress, play/download results. Single file, no framework, no build step. |
| API server | `backend/app.py` | Job creation/queueing, in-memory job store, status endpoint, file serving, static hosting of the frontend. |
| Pipeline worker | `backend/worker.py` | The actual processing: download → separate → encode → cleanup. Pure functions, no knowledge of HTTP or the job store. |

The worker communicates upward through a single `set_status(stage, progress)` callback, keeping it independently testable and reusable (e.g. a future CLI could call `run_pipeline` directly).

## 3. Processing pipeline

`run_pipeline(url, job_dir, set_status)` executes four stages sequentially:

### 3.1 Download — `download_audio`
- **Tool:** `yt-dlp` (Python API), format `bestaudio/best`, `noplaylist=True`.
- Output template `source.%(ext)s` — typically yields `source.webm` (Opus) or `source.m4a`.
- Progress: yt-dlp progress hooks report `downloaded_bytes / total_bytes` (estimate used as fallback).
- Also captures the video title for display and download filenames.

### 3.2 Separation — `separate`
- **Tool:** Demucs `htdemucs` model, `--two-stems vocals`, run as a subprocess of the venv's own interpreter (`sys.executable -m demucs`).
- Subprocess rather than in-process API: isolates torch memory usage to a process that fully exits per job, and lets us kill/restart cleanly.
- Outputs `no_vocals.wav` + `vocals.wav` under `stems/htdemucs/source/`.
- **Progress parsing:** demucs prints a tqdm bar to stderr counting *seconds of audio processed* (`117.0/257.4`). tqdm redraws with `\r`, so stderr is read in raw chunks and split on `[\r\n]`. Lines are filtered to those containing `"second"` — this excludes the model-download tqdm bar (measured in MB) that appears on first run. The fraction maps directly to progress 0–1.
- The last 10 stderr lines are retained in a ring buffer (`deque(maxlen=10)`) so a failure surfaces a useful error, not a bare exit code.

### 3.3 Encoding — `encode_mp3` (×3)
- **Tool:** ffmpeg, `libmp3lame`, **320 kbps CBR**.
- Encodes both stems and the original source audio to MP3.
- 320k chosen after user preference; note the true quality ceiling is the YouTube source (~130–160 kbps Opus) and separation artifacts, not the encode.

### 3.4 Cleanup
- Deletes the `stems/` directory (two full-length WAVs, ~100 MB+) and the downloaded source file.
- Final disk footprint per job: three MP3s (~7–10 MB each for a typical song).

### Error handling
- Every subprocess failure raises with the *tail of stderr* (last 10 lines): ffmpeg via `_run_checked`, demucs via the streaming ring buffer.
- `_run_job` in the server catches any pipeline exception and stores it on the job (`stage="error"`, `error=<message>`), so failures reach the UI instead of dying silently in the worker thread.
- Design rationale: an early bug shipped `capture_output=True` + bare `CalledProcessError`, which produced an undiagnosable "exit status 1" (root cause: missing `numpy`). The stderr-tail rule exists so that class of failure can't recur.

## 4. Job model & concurrency

### Job store
In-memory `dict[str, dict]` guarded by a `threading.Lock`:

```python
{
  "<job_id>": {
    "stage":    "queued" | "downloading" | "separating" | "encoding" | "done" | "error",
    "progress": float | None,   # 0–1, None = indeterminate (encoding)
    "title":    str | None,     # video title, set on completion
    "error":    str | None,     # stderr tail / exception message
    "sizes":    {"karaoke": int, "vocals": int, "original": int},  # bytes, set on completion
  }
}
```

- Job IDs: `uuid4().hex[:12]` — unguessable enough for a local tool, short enough for URLs.
- The store is intentionally not persisted: jobs are cheap to re-run, and a restart with no DB keeps operations trivial. MP3s survive restarts on disk but become unaddressable via the API (accepted trade-off; see §7).

### Concurrency model
- **`ThreadPoolExecutor(max_workers=1)`** — exactly one pipeline runs at a time; additional submissions queue FIFO and sit in `stage="queued"`.
- Rationale: demucs saturates CPU/GPU; two concurrent separations would both slow to less than half speed and can exhaust memory. Serial execution is also the simplest mental model for a single-user tool.
- Threads (not asyncio) because the workload is subprocess- and blocking-IO-bound; FastAPI's sync endpoints run in its own threadpool and touch the store only under the lock.

## 5. API design

| Method | Path | Request | Response |
|---|---|---|---|
| `POST` | `/api/jobs` | `{"url": str}` | `201-ish {"id": str}` — returns immediately; work happens in background |
| `GET` | `/api/jobs/{id}` | — | Full job record (schema above) + `id`. `404` if unknown. |
| `GET` | `/api/jobs/{id}/{track}.mp3` | `track ∈ {karaoke, vocals, original}` | `FileResponse`, `audio/mpeg`, download filename `"<title> (<track>).mp3"`. `404` if track unknown, job unknown, or file not yet produced. |
| `GET` | `/` (and any static path) | — | `StaticFiles` mount serving `frontend/` (`html=True`). Mounted last so `/api/*` wins routing. |

### Status transport: polling, not SSE/WebSockets
The frontend polls `GET /api/jobs/{id}` every 1.5 s. At one active job and a handful of stage changes per job, polling costs are negligible, works through any proxy, and keeps both ends stateless. SSE/WebSockets would add connection lifecycle handling for no visible UX gain at this scale.

Known limitation: FastAPI does not auto-register `HEAD` handlers, so `HEAD` on the mp3 endpoints returns 404. File sizes are therefore delivered in the status payload (`sizes`) rather than sniffed by the client — which also happens to be one fewer round-trip.

## 6. Frontend design

- **Single HTML file, vanilla JS** (~150 lines). No framework/build: the UI is one form, one status line, one `<progress>`, three `<audio>` players.
- State machine mirrors the backend stages; `STAGE_LABELS` maps them to human text. `progress != null` → determinate bar + percentage in the status line; `null` → bar hidden (indeterminate stages).
- Results section: per track an inline `<audio controls>` player and a download link labeled with the human-formatted size (`9.7 MB`), computed from `sizes` in the final status payload.
- Errors (submit failure, poll failure, `stage="error"`) render the message in red and re-enable the form.
- `color-scheme: light dark` for automatic dark-mode support.

## 7. Storage & lifecycle

```
data/jobs/<job_id>/
├── source.webm        # transient, deleted on success
├── stems/…            # transient, deleted on success
├── karaoke.mp3        # kept
├── vocals.mp3         # kept
└── original.mp3       # kept
```

- `data/` is gitignored; directories are created on demand, so a fresh clone needs no setup beyond `pip install`.
- **No automatic cleanup**: completed job folders accumulate (~25–30 MB each) until manually deleted. On failure, transient files may also remain. Acceptable for a personal tool; flagged as future work.

## 8. Security posture

Scoped as a **localhost, single-user** tool:
- No authentication or rate limiting; anyone who can reach the port can submit jobs and read any job's files (IDs are unguessable but this is not relied on).
- The URL is passed to yt-dlp, which will fetch arbitrary user-supplied URLs (SSRF-equivalent) — irrelevant locally, unacceptable if ever exposed.
- No `--host 0.0.0.0` in the documented run command, deliberately.

**If this were ever deployed beyond localhost**, it would need: auth, per-user job namespacing, URL allow-listing (YouTube domains only), duration caps, disk quotas, and rate limiting. That is explicitly out of scope.

## 9. Dependencies

| Dependency | Role | Notes |
|---|---|---|
| `fastapi` + `uvicorn[standard]` | HTTP server | Sync endpoints; static file serving |
| `yt-dlp` | YouTube download | Needs periodic upgrades as YouTube changes |
| `demucs` (+ `torch`, `torchaudio`) | Vocal separation | `htdemucs` weights (~80 MB) downloaded to `~/.cache` on first run |
| `numpy` | Required by demucs at import time | **Not** pulled in transitively on this setup — must stay pinned in requirements |
| `ffmpeg` (system) | MP3 encode; also used internally by yt-dlp/demucs | Installed via Homebrew, not pip |

Versions are currently unpinned (latest-at-install). Pinning via `pip freeze` is listed as future work.

## 10. Testing strategy

Current: verified manually/by scripted checks —
- Endpoint smoke tests (static serving, 404s, bad-URL job → `stage="error"`).
- Real demucs run asserting progress callbacks are monotonic 0→1 and stems exist.
- `TestClient` + mocked `run_pipeline` asserting the done-payload shape (`sizes` etc.).

Planned (not yet implemented): commit those checks as a pytest suite — the `TestClient`/mock pattern already proven; pipeline tests use a small local audio fixture instead of hitting YouTube.

## 11. Future work (priority order)

1. **Initial git commit** — repo currently has zero commits.
2. **Disk cleanup** — delete job folders older than N days at startup.
3. **Duration cap** — reject videos over ~15 min via yt-dlp metadata pre-check, before download.
4. **Session resilience** — persist job ID in URL hash/localStorage so a page refresh re-attaches to a running job.
5. Duplicate detection (key jobs by YouTube video ID; serve cached results instantly).
6. Queue position in status (`"queued (2 ahead)"`).
7. Pinned requirements + pytest suite (§10).
