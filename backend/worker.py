"""Pipeline: YouTube URL -> downloaded audio -> demucs stems -> karaoke + vocals mp3s.

The Cut & Mix studio that used to live here is now its own app:
https://github.com/budhap-dev/music-mixer
"""

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

# Matches a tqdm percentage, e.g. " 47%|".
_TQDM_PERCENT = re.compile(r"(\d+)%\|")


def _run_streaming(cmd: list[str], on_line) -> None:
    """Run a command, streaming its output line by line to on_line (tqdm redraws
    with \r, so raw chunks are split on \r or \n). Raises with the stderr tail
    on failure, like _run_checked."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail: deque[str] = deque(maxlen=10)
    buf = ""
    while chunk := proc.stdout.read(256):
        buf += chunk
        *lines, buf = re.split(r"[\r\n]", buf)
        for line in lines:
            if not line.strip():
                continue
            tail.append(line.strip())
            on_line(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed (exit {proc.returncode}):\n" + "\n".join(tail))


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
    def on_line(line: str) -> None:
        # Only the separation bar counts in seconds — skip e.g. the model-download bar.
        m = _TQDM_FRACTION.search(line) if "second" in line else None
        if m and float(m.group(2)) > 0:
            on_progress(min(float(m.group(1)) / float(m.group(2)), 1.0))

    _run_streaming(cmd, on_line)
    stem_dir = out_dir / DEMUCS_MODEL / audio.stem
    return stem_dir / "no_vocals.wav", stem_dir / "vocals.wav"


def encode_mp3(wav: Path, mp3: Path) -> None:
    _run_checked(["ffmpeg", "-y", "-i", str(wav), *MP3_ARGS, str(mp3)])


KARAOKE_MODEL = "UVR_MDXNET_KARA_2.onnx"  # UVR "karaoke" model: removes lead vocal only
MODEL_CACHE = Path.home() / ".cache" / "audio-separator"


def make_chorus_karaoke(source_audio: Path, job_dir: Path, mp3: Path, on_progress) -> None:
    """Karaoke that keeps backing/chorus vocals.

    Runs a dedicated lead-vocal separation model (UVR MDX-Net Karaoke) on the
    original mix; its "Instrumental" output is music + backing vocals with only
    the lead removed. Unlike centre-channel tricks this works even when the
    lead has wide doubling or stereo reverb.
    """
    work = job_dir / "chorus"
    work.mkdir(exist_ok=True)
    try:
        wav = work / "input.wav"  # the separator reads wav/flac/mp3, not webm
        _run_checked(["ffmpeg", "-y", "-i", str(source_audio), str(wav)])
        # The MDX model runs two full passes over the audio, each with its own
        # 0-100% bar; fold them into one continuous bar.
        passes = 2
        state = {"pass": 0, "last": 0.0}

        def on_line(line: str) -> None:
            m = _TQDM_PERCENT.search(line)
            if not m:
                return
            pct = int(m.group(1)) / 100
            if pct < state["last"] - 0.5:  # bar reset -> next pass started
                state["pass"] = min(state["pass"] + 1, passes - 1)
            state["last"] = pct
            on_progress(min((state["pass"] + pct) / passes, 1.0))

        _run_streaming([
            str(Path(sys.executable).parent / "audio-separator"), str(wav),
            "--model_filename", KARAOKE_MODEL,
            "--model_file_dir", str(MODEL_CACHE),
            "--output_dir", str(work),
            "--output_format", "WAV",
        ], on_line)
        instrumental = next(work.glob("*_(Instrumental)_*.wav"))
        encode_mp3(instrumental, mp3)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def probe_formats(url: str) -> dict:
    """Title, 320k-MP3 size estimate, and available MP4 heights with size
    estimates (video stream + audio stream), without downloading."""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats", [])

    def fsize(f):
        return f.get("filesize") or f.get("filesize_approx")

    # audio stream size, for the mp4 merge estimate (prefer m4a — that's what we fetch)
    audio = [f for f in formats
             if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    m4a = [f for f in audio if f.get("ext") == "m4a"] or audio
    audio_size = max((fsize(f) or 0 for f in m4a), default=0)

    # per height: size of the best matching video stream (avc1 preferred, like the download)
    by_height: dict[int, dict] = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        pref = str(f.get("vcodec", "")).startswith("avc1")
        size = fsize(f)
        cur = by_height.get(h)
        if cur is None or (pref, size or 0) > (cur["pref"], cur["size"] or 0):
            by_height[h] = {"pref": pref, "size": size}

    heights = [
        {"height": h, "size": (v["size"] + audio_size) if v["size"] else None}
        for h, v in sorted(by_height.items(), reverse=True)
    ]
    # 320 kbps CBR = 40 KB per second
    duration = info.get("duration")
    mp3_size = int(duration * 40_000) if duration else None
    return {"title": info.get("title", "Unknown title"), "mp3_size": mp3_size, "heights": heights}


def download_mp3(url: str, job_dir: Path, set_status) -> dict:
    """Just the audio, as 320 kbps MP3 — no separation."""
    job_dir.mkdir(parents=True, exist_ok=True)
    set_status("downloading", 0.0)
    audio, title = download_audio(url, job_dir, lambda p: set_status("downloading", p))
    set_status("encoding", None)
    mp3 = job_dir / "download.mp3"
    encode_mp3(audio, mp3)
    audio.unlink(missing_ok=True)
    return {"title": title, "file": mp3}


def download_mp4(url: str, height: int, job_dir: Path, set_status) -> dict:
    """Video+audio at up to the requested height, merged into an MP4."""
    job_dir.mkdir(parents=True, exist_ok=True)
    set_status("downloading", 0.0)

    # Video and audio download as separate files, each with its own 0-100%;
    # fold them into one bar (video dominates, audio phase is quick).
    state = {"phase": 0, "last": 0.0}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if not total:
                return
            p = d.get("downloaded_bytes", 0) / total
            if p < state["last"] - 0.5:
                state["phase"] = 1
            state["last"] = p
            set_status("downloading", min((state["phase"] + p) / 2, 1.0))
        elif d["status"] == "finished" and state["phase"] == 1:
            set_status("merging", None)

    opts = {
        # Prefer H.264 + AAC so the .mp4 plays everywhere (Safari/QuickTime
        # reject VP9/Opus remuxed into mp4); fall back to whatever exists.
        "format": (
            f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={height}][ext=mp4]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(job_dir / "download.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return {"title": info.get("title", "Unknown title"), "file": job_dir / "download.mp4"}


def run_pipeline(url: str, job_dir: Path, set_status, keep_chorus: bool = False) -> dict:
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
    if keep_chorus:
        set_status("separating_chorus", 0.0)
        make_chorus_karaoke(audio, job_dir, job_dir / "karaoke_chorus.mp3",
                            lambda p: set_status("separating_chorus", p))

    # Drop the big intermediates; keep only the mp3s.
    shutil.rmtree(job_dir / "stems", ignore_errors=True)
    audio.unlink(missing_ok=True)

    return {"title": title, "karaoke": karaoke_mp3, "vocals": vocals_mp3, "original": original_mp3}
