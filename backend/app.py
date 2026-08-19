"""FastAPI server: submit YouTube jobs, upload mp3s, cut & mix the results, serve the mp3s."""

import json
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import worker

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"
FRONTEND_DIR = ROOT / "frontend"

TRACKS = ("karaoke", "vocals", "original", "mix")
_JOB_ID = re.compile(r"[0-9a-f]{12}")

app = FastAPI(title="Karaoke Maker")

# Demucs is CPU/GPU heavy — run one job at a time, extra jobs wait in queue.
executor = ThreadPoolExecutor(max_workers=1)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class JobRequest(BaseModel):
    url: str


class Clip(BaseModel):
    job: str
    track: str
    start: float | None = Field(None, ge=0)  # cut in the source, seconds
    end: float | None = Field(None, gt=0)
    offset: float = Field(0, ge=0)  # placement on the output timeline, seconds
    gain: float = Field(1.0, ge=0, le=10)


class EQ(BaseModel):
    bass: float = Field(0, ge=-12, le=12)  # dB
    mid: float = Field(0, ge=-12, le=12)
    treble: float = Field(0, ge=-12, le=12)


class MixRequest(BaseModel):
    clips: list[Clip] = Field(min_length=1, max_length=16)
    title: str | None = None
    eq: EQ | None = None
    enhance: bool = False


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"stage": "queued", "progress": None, "title": None, "error": None, "sizes": None}
    return job_id


def _set_status(job_id: str, stage: str, progress: float | None) -> None:
    with jobs_lock:
        jobs[job_id].update(stage=stage, progress=progress)


def _load_meta(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(job_dir: Path, **updates) -> None:
    meta = _load_meta(job_dir) | updates
    (job_dir / "meta.json").write_text(json.dumps(meta))


def _finish(job_id: str, job_dir: Path, title: str) -> None:
    _save_meta(job_dir, title=title)
    sizes = {t: p.stat().st_size for t in TRACKS if (p := job_dir / f"{t}.mp3").exists()}
    with jobs_lock:
        jobs[job_id].update(stage="done", progress=1.0, title=title, sizes=sizes)


def _fail(job_id: str, exc: Exception) -> None:
    with jobs_lock:
        jobs[job_id].update(stage="error", error=str(exc))


def _run_job(job_id: str, url: str) -> None:
    job_dir = JOBS_DIR / job_id
    try:
        result = worker.run_pipeline(url, job_dir, lambda s, p: _set_status(job_id, s, p))
        _finish(job_id, job_dir, result["title"])
    except Exception as exc:  # noqa: BLE001 — report any pipeline failure to the client
        _fail(job_id, exc)


def _run_mix(job_id: str, req: MixRequest) -> None:
    job_dir = JOBS_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        _set_status(job_id, "mixing", None)
        clips = [
            {
                "path": JOBS_DIR / c.job / f"{c.track}.mp3",
                "start": c.start, "end": c.end, "offset": c.offset, "gain": c.gain,
            }
            for c in req.clips
        ]
        worker.make_mix(
            clips, job_dir / "mix.mp3",
            eq=req.eq.model_dump() if req.eq else None,
            enhance=req.enhance,
        )
        _finish(job_id, job_dir, req.title or f"Mix ({len(clips)} clips)")
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, exc)


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    job_id = _new_job()
    executor.submit(_run_job, job_id, req.url)
    return {"id": job_id}


@app.post("/api/uploads")
def upload(file: UploadFile) -> dict:
    """Add the user's own audio file to the library (stored as its job's original.mp3)."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    raw = job_dir / "upload.bin"  # ffmpeg sniffs the container from content, not extension
    try:
        with raw.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        worker.encode_mp3(raw, job_dir / "original.mp3")  # transcode = validate + normalize
    except RuntimeError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"could not read that file as audio: {exc}") from exc
    finally:
        raw.unlink(missing_ok=True)
    title = Path(file.filename or "").stem or f"Upload {job_id}"
    _save_meta(job_dir, title=title)
    return {"id": job_id, "title": title}


@app.post("/api/mixes")
def create_mix(req: MixRequest) -> dict:
    for c in req.clips:
        if c.track not in TRACKS or not _JOB_ID.fullmatch(c.job):
            raise HTTPException(400, f"invalid clip source: {c.job}/{c.track}")
        if not (JOBS_DIR / c.job / f"{c.track}.mp3").exists():
            raise HTTPException(404, f"clip source not found: {c.job}/{c.track}.mp3")
        if c.start is not None and c.end is not None and c.end <= c.start:
            raise HTTPException(400, "clip end must be after its start")
    job_id = _new_job()
    executor.submit(_run_mix, job_id, req)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {"id": job_id, **job}


@app.get("/api/library")
def library() -> list[dict]:
    """All finished tracks on disk, with durations — survives server restarts."""
    items = []
    if JOBS_DIR.exists():
        for d in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not (d.is_dir() and _JOB_ID.fullmatch(d.name)):
                continue
            meta = _load_meta(d)
            durations = meta.get("durations", {})
            tracks = []
            for t in TRACKS:
                path = d / f"{t}.mp3"
                if not path.exists():
                    continue
                if t not in durations:
                    durations[t] = worker.probe_duration(path)
                tracks.append({"track": t, "duration": durations[t]})
            if tracks:
                if durations != meta.get("durations", {}):
                    _save_meta(d, durations=durations)  # probe once, cache in meta.json
                items.append({"job": d.name, "title": meta.get("title") or d.name, "tracks": tracks})
    return items


@app.get("/api/jobs/{job_id}/{track}.mp3")
def get_track(job_id: str, track: str) -> FileResponse:
    if track not in TRACKS or not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "unknown track")
    path = JOBS_DIR / job_id / f"{track}.mp3"
    if not path.exists():
        raise HTTPException(404, "file not ready")
    title = _load_meta(path.parent).get("title") or job_id
    return FileResponse(path, media_type="audio/mpeg", filename=f"{title} ({track}).mp3")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
