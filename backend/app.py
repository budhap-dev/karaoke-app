"""FastAPI server: submit a YouTube URL, poll job status, fetch the resulting mp3s."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import worker

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Karaoke Maker")

# Demucs is CPU/GPU heavy — run one job at a time, extra jobs wait in queue.
executor = ThreadPoolExecutor(max_workers=1)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class JobRequest(BaseModel):
    url: str


def _set_status(job_id: str, stage: str, progress: float | None) -> None:
    with jobs_lock:
        jobs[job_id].update(stage=stage, progress=progress)


def _run_job(job_id: str, url: str) -> None:
    job_dir = JOBS_DIR / job_id
    try:
        result = worker.run_pipeline(url, job_dir, lambda s, p: _set_status(job_id, s, p))
        sizes = {t: result[t].stat().st_size for t in ("karaoke", "vocals", "original")}
        with jobs_lock:
            jobs[job_id].update(stage="done", progress=1.0, title=result["title"], sizes=sizes)
    except Exception as exc:  # noqa: BLE001 — report any pipeline failure to the client
        with jobs_lock:
            jobs[job_id].update(stage="error", error=str(exc))


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"stage": "queued", "progress": None, "title": None, "error": None}
    executor.submit(_run_job, job_id, req.url)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {"id": job_id, **job}


@app.get("/api/jobs/{job_id}/{track}.mp3")
def get_track(job_id: str, track: str) -> FileResponse:
    if track not in ("karaoke", "vocals", "original"):
        raise HTTPException(404, "unknown track")
    path = JOBS_DIR / job_id / f"{track}.mp3"
    if job_id not in jobs or not path.exists():
        raise HTTPException(404, "file not ready")
    with jobs_lock:
        title = jobs[job_id].get("title") or job_id
    return FileResponse(path, media_type="audio/mpeg", filename=f"{title} ({track}).mp3")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
