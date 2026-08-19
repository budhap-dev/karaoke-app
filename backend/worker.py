"""Pipeline: YouTube URL -> downloaded audio -> demucs stems -> karaoke + vocals mp3s."""

import re
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path

import yt_dlp

DEMUCS_MODEL = "htdemucs"

MP3_ARGS = ["-codec:a", "libmp3lame", "-b:a", "320k"]


def _run_checked(cmd: list[str]) -> None:
    """Run a command; on failure raise with the stderr tail so the job error is diagnosable."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-10:])
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}):\n{tail}")


def download_audio(url: str, job_dir: Path, on_progress) -> tuple[Path, str]:
    """Download best audio from a YouTube URL. Returns (audio_path, video_title)."""

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(d.get("downloaded_bytes", 0) / total)
        elif d["status"] == "finished":
            on_progress(1.0)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    audio = next(job_dir.glob("source.*"))
    return audio, info.get("title", "Unknown title")


# Matches the processed/total fraction in demucs's tqdm bar, e.g. "117.0/257.4".
_TQDM_FRACTION = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")


def separate(audio: Path, job_dir: Path, on_progress) -> tuple[Path, Path]:
    """Run demucs two-stem separation. Returns (karaoke_wav, vocals_wav)."""
    out_dir = job_dir / "stems"
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-n", DEMUCS_MODEL,
        "-o", str(out_dir),
        str(audio),
    ]
    # tqdm redraws its bar with \r on stderr, so read raw chunks and split on \r or \n.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    tail: deque[str] = deque(maxlen=10)
    buf = ""
    while chunk := proc.stderr.read(256):
        buf += chunk
        *lines, buf = re.split(r"[\r\n]", buf)
        for line in lines:
            if not line.strip():
                continue
            tail.append(line.strip())
            # Only the separation bar counts in seconds — skip e.g. the model-download bar.
            m = _TQDM_FRACTION.search(line) if "second" in line else None
            if m and float(m.group(2)) > 0:
                on_progress(min(float(m.group(1)) / float(m.group(2)), 1.0))
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"demucs failed (exit {proc.returncode}):\n" + "\n".join(tail))
    stem_dir = out_dir / DEMUCS_MODEL / audio.stem
    return stem_dir / "no_vocals.wav", stem_dir / "vocals.wav"


def encode_mp3(wav: Path, mp3: Path) -> None:
    _run_checked(["ffmpeg", "-y", "-i", str(wav), *MP3_ARGS, str(mp3)])


def make_mix(clips: list[dict], out_mp3: Path, eq: dict | None = None, enhance: bool = False) -> None:
    """Cut and layer audio clips into a single mp3.

    Each clip: {"path": Path, "start": float|None, "end": float|None,
    "offset": float, "gain": float}. start/end cut the source; offset places
    the cut on the output timeline. Overlapping clips blend together,
    back-to-back offsets form a medley.

    eq: optional master EQ, {"bass": dB, "mid": dB, "treble": dB}.
    enhance: master clarity chain — rumble cut, presence lift, loudness
    normalization (also prevents clipping where loud clips overlap).
    """
    cmd = ["ffmpeg", "-y"]
    filters = []
    for i, clip in enumerate(clips):
        cmd += ["-i", str(clip["path"])]
        steps = []
        if clip.get("start") is not None or clip.get("end") is not None:
            args = []
            if clip.get("start") is not None:
                args.append(f"start={clip['start']}")
            if clip.get("end") is not None:
                args.append(f"end={clip['end']}")
            steps += ["atrim=" + ":".join(args), "asetpts=PTS-STARTPTS"]
        if clip.get("offset"):
            steps.append(f"adelay={int(clip['offset'] * 1000)}:all=1")
        if clip.get("gain", 1) != 1:
            steps.append(f"volume={clip['gain']}")
        steps = steps or ["anull"]
        filters.append(f"[{i}:a]" + ",".join(steps) + f"[c{i}]")
    master = []
    if eq:
        if eq.get("bass"):
            master.append(f"bass=g={eq['bass']}")
        if eq.get("mid"):
            master.append(f"equalizer=f=1000:t=q:w=1.0:g={eq['mid']}")
        if eq.get("treble"):
            master.append(f"treble=g={eq['treble']}")
    if enhance:
        # rumble cut, vocal presence, broadcast loudness; loudnorm outputs 192kHz,
        # so resample back down for the mp3 encoder.
        master += ["highpass=f=60", "equalizer=f=3200:t=q:w=1.0:g=2",
                   "loudnorm=I=-14:TP=-1.5:LRA=11", "aresample=44100"]
    joined = "".join(f"[c{i}]" for i in range(len(clips)))
    mix_out = "[mixed]" if master else "[out]"
    filters.append(f"{joined}amix=inputs={len(clips)}:duration=longest:normalize=0{mix_out}")
    if master:
        filters.append("[mixed]" + ",".join(master) + "[out]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", *MP3_ARGS, str(out_mp3)]
    _run_checked(cmd)


def run_pipeline(url: str, job_dir: Path, set_status) -> dict:
    """Full pipeline. set_status(stage, progress) reports to the job store.

    Returns {"title": ..., "karaoke": path, "vocals": path, "original": path}.
    """
    job_dir.mkdir(parents=True, exist_ok=True)

    set_status("downloading", 0.0)
    audio, title = download_audio(url, job_dir, lambda p: set_status("downloading", p))

    set_status("separating", 0.0)
    karaoke_wav, vocals_wav = separate(audio, job_dir, lambda p: set_status("separating", p))

    set_status("encoding", None)
    karaoke_mp3 = job_dir / "karaoke.mp3"
    vocals_mp3 = job_dir / "vocals.mp3"
    original_mp3 = job_dir / "original.mp3"
    encode_mp3(karaoke_wav, karaoke_mp3)
    encode_mp3(vocals_wav, vocals_mp3)
    encode_mp3(audio, original_mp3)

    # Drop the big intermediates; keep only the mp3s.
    shutil.rmtree(job_dir / "stems", ignore_errors=True)
    audio.unlink(missing_ok=True)

    return {"title": title, "karaoke": karaoke_mp3, "vocals": vocals_mp3, "original": original_mp3}


def probe_duration(path: Path) -> float:
    """Audio duration in seconds via ffprobe (0.0 if unreadable)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return round(float(proc.stdout.strip()), 2)
    except ValueError:
        return 0.0
