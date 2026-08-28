"""FastAPI server: submit a YouTube URL, poll job status, fetch the resulting mp3s."""

import json
import re
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

TRACKS = ("karaoke", "karaoke_chorus", "vocals", "original")
_JOB_ID = re.compile(r"[0-9a-f]{12}")

app = FastAPI(title="Karaoke Maker")

# Demucs is CPU/GPU heavy — run one job at a time, extra jobs wait in queue.
executor = ThreadPoolExecutor(max_workers=1)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class JobRequest(BaseModel):
    url: str
    keep_chorus: bool = False  # also produce karaoke_chorus.mp3 (backing vocals kept)


def _set_status(job_id: str, stage: str, progress: float | None) -> None:
    with jobs_lock:
        jobs[job_id].update(stage=stage, progress=progress)


def _title_of(job_dir: Path) -> str | None:
    try:
        return json.loads((job_dir / "meta.json").read_text()).get("title")
    except (OSError, json.JSONDecodeError):
        return None


def _run_job(job_id: str, url: str, keep_chorus: bool) -> None:
    job_dir = JOBS_DIR / job_id
    try:
        result = worker.run_pipeline(url, job_dir, lambda s, p: _set_status(job_id, s, p), keep_chorus=keep_chorus)
        (job_dir / "meta.json").write_text(json.dumps({"title": result["title"]}))
        sizes = {t: p.stat().st_size for t in TRACKS if (p := job_dir / f"{t}.mp3").exists()}
        with jobs_lock:
            jobs[job_id].update(stage="done", progress=1.0, title=result["title"], sizes=sizes)
    except Exception as exc:  # noqa: BLE001 — report any pipeline failure to the client
        with jobs_lock:
            jobs[job_id].update(stage="error", error=str(exc))


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"stage": "queued", "progress": None, "title": None, "error": None, "sizes": None}
    executor.submit(_run_job, job_id, req.url, req.keep_chorus)
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
    if track not in TRACKS or not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "unknown track")
    path = JOBS_DIR / job_id / f"{track}.mp3"
    if not path.exists():
        raise HTTPException(404, "file not ready")
    title = _title_of(path.parent) or job_id
    return FileResponse(path, media_type="audio/mpeg", filename=f"{title} ({track}).mp3")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
