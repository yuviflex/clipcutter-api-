import os
import random
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, jsonify, make_response, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

_sys_rng = random.SystemRandom()

BASE_DIR = Path.home() / "clipcutter"
LOG_FILE = BASE_DIR / "api.log"
JOBS_DIR = BASE_DIR / "jobs"
GALLERY_DIR = Path("/sdcard/Movies/ClipCutter")

BASE_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
try:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

JOBS = {}
JOBS_LOCK = threading.Lock()
RECENT_STARTS = []
RECENT_LOCK = threading.Lock()


def log_msg(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fmt_hms(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_sec(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return max(0, int(val))
    parts = str(val).strip().split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return max(0, nums[0])
    return None


def set_job(job_id: str, **updates):
    with JOBS_LOCK:
        cur = JOBS.get(job_id, {"job_id": job_id, "status": "running", "progress": 5, "stage": "starting"})
        cur.update(updates)
        JOBS[job_id] = cur
        return dict(cur)


def get_job(job_id: str):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


def run_cut_worker(job_id: str, payload: dict):
    out_dir = Path(payload.get("gallery_dir") or str(GALLERY_DIR))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        out_dir = JOBS_DIR

    action = (payload.get("action") or "auto_clip_download").strip()
    idx = int(payload.get("index") or 1)
    url = (payload.get("url") or "").strip()
    local_file = (payload.get("file") or "").strip()
    start = (payload.get("start") or "00:00:00").strip()
    end = (payload.get("end") or "00:00:25").strip()
    dur = int(payload.get("duration") or 25)
    fmt = (payload.get("format") or "split").strip()

    log_msg(f"JOB {job_id} START: action={action} start={start} end={end} url={url or local_file}")
    set_job(job_id, status="running", progress=15, stage="preparing")

    if not shutil.which("ffmpeg"):
        set_job(job_id, status="error", progress=100, stage="error", error="ffmpeg not installed in Termux")
        log_msg(f"JOB {job_id} FAILED: ffmpeg missing")
        return

    if action == "local_trim" and local_file:
        src = Path(local_file)
        if not src.exists():
            alt = Path("/sdcard/Download") / src.name
            if alt.exists():
                src = alt
        if not src.exists():
            set_job(job_id, status="error", progress=100, stage="error", error=f"Local file not found: {src}")
            log_msg(f"JOB {job_id} FAILED: local file not found {src}")
            return

        final_mp4 = out_dir / f"local_trim_{job_id[:8]}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", start,
            "-to", end,
            "-i", str(src),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(final_mp4),
        ]
        set_job(job_id, progress=45, stage="trimming_local")
        proc = subprocess.run(cmd, capture_output=True, text=True)
    else:
        if not shutil.which("yt-dlp"):
            set_job(job_id, status="error", progress=100, stage="error", error="yt-dlp not installed in Termux")
            log_msg(f"JOB {job_id} FAILED: yt-dlp missing")
            return
        if not url:
            set_job(job_id, status="error", progress=100, stage="error", error="YouTube URL missing")
            log_msg(f"JOB {job_id} FAILED: url missing")
            return

        raw_mp4 = out_dir / f"sec_{job_id[:8]}.mp4"
        out_name = (
            f"manual_cut_{job_id[:8]}.mp4"
            if action == "manual_cut"
            else f"clip_{idx}_{dur}s_{fmt}_{job_id[:8]}.mp4"
        )
        final_mp4 = out_dir / out_name

        dl_cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "-f", "best[height<=720][ext=mp4]/best[ext=mp4]/best",
            "--download-sections", f"*{start}-{end}",
            "--force-keyframes-at-cuts",
            "--no-playlist",
            "-o", str(raw_mp4),
            url,
        ]
        set_job(job_id, progress=30, stage="downloading_section")
        proc = subprocess.run(dl_cmd, capture_output=True, text=True)

        if proc.returncode == 0:
            input_for_ff = raw_mp4 if raw_mp4.exists() else next(out_dir.glob(f"sec_{job_id[:8]}*"), None)
            if input_for_ff and Path(input_for_ff).exists():
                set_job(job_id, progress=72, stage="encoding_mp4")
                ff_cmd = [
                    "ffmpeg", "-y",
                    "-i", str(input_for_ff),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                    "-c:a", "aac", "-b:a", "128k",
                    str(final_mp4),
                ]
                proc = subprocess.run(ff_cmd, capture_output=True, text=True)
                try:
                    Path(input_for_ff).unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                set_job(job_id, status="error", progress=100, stage="error", error="Downloaded section file not found")
                log_msg(f"JOB {job_id} FAILED: section file not found")
                return

    if proc.returncode == 0 and final_mp4.exists() and final_mp4.stat().st_size > 0:
        if shutil.which("termux-media-scan"):
            subprocess.run(["termux-media-scan", str(final_mp4)], capture_output=True, text=True)
        set_job(job_id, status="completed", progress=100, stage="completed", output=str(final_mp4))
        log_msg(f"JOB {job_id} COMPLETED -> {final_mp4}")
    else:
        err_tail = (proc.stderr or proc.stdout or "Command failed").strip()[-350:]
        set_job(job_id, status="error", progress=100, stage="error", error=err_tail)
        log_msg(f"JOB {job_id} FAILED: {err_tail}")


@app.get("/")
@app.get("/health")
def health():
    resp = make_response(jsonify({"status": "ok", "service": "clipcutter-main", "ts": int(time.time() * 1000)}))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.post("/moments")
@app.post("/auto-moments")
@app.post("/clips")
@app.post("/analyze")
@app.post("/generate")
def moments():
    data = request.get_json(silent=True) or {}
    count = max(1, min(10, int(data.get("count") or data.get("num_clips") or 3)))
    dur = max(10, min(180, int(data.get("duration") or data.get("clip_duration") or 25)))
    max_start = max(120, min(7200, int(data.get("max_start") or 1800)))

    extra_avoid = []
    for item in (data.get("previous_clips") or data.get("exclude") or []):
        s = parse_sec(item.get("start") if isinstance(item, dict) else item)
        if s is not None:
            extra_avoid.append(s)

    with RECENT_LOCK:
        avoid = list(RECENT_STARTS[-40:]) + extra_avoid

    clips = []
    picked = []

    for i in range(count):
        start_sec = _sys_rng.randint(15, max_start)
        tries = 0
        while (
            any(abs(start_sec - u) < dur + 20 for u in picked)
            or any(abs(start_sec - a) < max(15, dur // 2) for a in avoid)
        ) and tries < 80:
            start_sec = _sys_rng.randint(15, max_start)
            tries += 1

        picked.append(start_sec)
        end_sec = start_sec + dur
        clips.append({
            "index": i + 1,
            "start": fmt_hms(start_sec),
            "end": fmt_hms(end_sec),
            "duration": dur,
        })

    with RECENT_LOCK:
        RECENT_STARTS.extend(picked)
        if len(RECENT_STARTS) > 100:
            del RECENT_STARTS[:-100]

    nonce = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    resp = make_response(jsonify({
        "ok": True,
        "nonce": nonce,
        "clips": clips,
    }))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.post("/exec")
@app.post("/cut")
def exec_job():
    data = request.get_json(silent=True) or {}
    job_id = f"job_{uuid.uuid4().hex[:10]}"
    set_job(job_id, status="running", progress=10, stage="queued", output="")
    t = threading.Thread(target=run_cut_worker, args=(job_id, data), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "status": "running", "progress": 10, "stage": "queued"})


@app.get("/status/<job_id>")
def job_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify(job)


@app.get("/download/<job_id>")
def job_download(job_id):
    job = get_job(job_id)
    out_path = job.get("output")
    if not out_path or not Path(out_path).exists():
        return jsonify({"ok": False, "error": "File not ready"}), 404
    return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name=Path(out_path).name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log_msg(f"Starting ClipCutter server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
