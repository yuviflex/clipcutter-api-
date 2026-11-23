import json
import os
import random
import re
import shutil
import subprocess
import tempfile
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

HYPE_PATTERN = re.compile(
    r"\b(clip|crazy|insane|wild|omg|wtf|lmao|lol|bruh|goat|clutch|no way|what|sheesh|sheru|fire|peak)\b|[🔥😂💀😭😱🤯 Chad|W|L]+",
    re.I,
)


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


def _extract_chat_events(chat_path: Path, video_start_ms: int | None):
    """Parse yt-dlp live_chat json/json3 lines into (sec, hype_weight, sample_text)."""
    events = []
    try:
        raw_text = chat_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return events

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        sec = None
        text_parts = []

        # YouTube live chat replay structure inside yt-dlp dump
        replay = obj.get("replayChatItemAction") or {}
        actions = replay.get("actions") or []
        offset_ms = replay.get("videoOffsetTimeMsec")
        if offset_ms is not None:
            try:
                sec = max(0, int(int(offset_ms) / 1000))
            except Exception:
                sec = None

        for act in actions:
            item = (act.get("addChatItemAction") or {}).get("item") or {}
            renderer = (
                item.get("liveChatTextMessageRenderer")
                or item.get("liveChatPaidMessageRenderer")
                or {}
            )
            if sec is None:
                ts_usec = renderer.get("timestampUsec")
                if ts_usec and video_start_ms:
                    try:
                        sec = max(0, int((int(ts_usec) // 1000 - video_start_ms) / 1000))
                    except Exception:
                        sec = None

            runs = (renderer.get("message") or {}).get("runs") or []
            for r in runs:
                if isinstance(r, dict):
                    if r.get("text"):
                        text_parts.append(str(r["text"]))
                    emoji_sc = ((r.get("emoji") or {}).get("shortcuts")) or []
                    if emoji_sc:
                        text_parts.append(" ".join(str(x) for x in emoji_sc))

        if sec is not None:
            msg = " ".join(text_parts).strip()
            weight = 1.0
            if msg and HYPE_PATTERN.search(msg):
                weight += 1.6
            if len(msg) >= 1 and sum(1 for ch in msg if ord(ch) > 127) >= 2:
                weight += 0.8
            events.append((sec, weight, msg[:45]))

    return events


def fetch_live_chat_candidates(yt_url: str, max_start: int):
    """Fetch YouTube live chat replay spikes using yt-dlp into a temporary folder."""
    candidates = []
    if not yt_url or not shutil.which("yt-dlp"):
        return candidates

    with tempfile.TemporaryDirectory(prefix="cc_chat_") as tmpdir:
        out_tpl = str(Path(tmpdir) / "chat")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--sub-langs", "live_chat",
            "--sub-format", "json3/json",
            "--no-warnings",
            "--no-playlist",
            "-o", out_tpl,
            yt_url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=18)
            chat_files = list(Path(tmpdir).glob("chat*"))
            if proc.returncode != 0 and not chat_files:
                return candidates

            all_events = []
            for cf in chat_files:
                all_events.extend(_extract_chat_events(cf, None))

            if not all_events:
                return candidates

            # Bucket chat hype into 10-second windows
            buckets = {}
            for sec, weight, sample in all_events:
                b = (sec // 10) * 10
                cur = buckets.get(b, {"score": 0.0, "count": 0, "sample": ""})
                cur["score"] += weight
                cur["count"] += 1
                if not cur["sample"] and sample:
                    cur["sample"] = sample
                buckets[b] = cur

            # Smooth across neighboring windows (prev + current + next)
            smoothed = []
            for b, info in buckets.items():
                neighbor_bonus = (
                    buckets.get(b - 10, {}).get("score", 0.0) * 0.5
                    + buckets.get(b + 10, {}).get("score", 0.0) * 0.5
                )
                total_hype = info["score"] + neighbor_bonus
                # Slightly lead before peak (-4s) so reaction/build-up isn't cut off
                start_sec = max(5, min(max_start, b - 4))
                reason = f"Chat spike ({info['count']} msgs)"
                if info["sample"]:
                    reason = f"{reason}: {info['sample']}"
                smoothed.append((total_hype, start_sec, reason))

            smoothed.sort(key=lambda x: x[0], reverse=True)
            top_hype = smoothed[0][0] if smoothed else 1.0

            for total_hype, s_sec, reason in smoothed[:25]:
                norm_score = int(75 + min(25, (total_hype / max(1.0, top_hype)) * 25))
                candidates.append({
                    "sec": s_sec,
                    "score": norm_score,
                    "reason": reason,
                })
        except Exception:
            return candidates

    return candidates


def fetch_video_moment_candidates(yt_url: str):
    """Extract chapters, heatmap peaks, description timestamps, and livestream chat spikes."""
    candidates = []
    max_start = 1800
    if not yt_url or not shutil.which("yt-dlp"):
        return max_start, candidates

    try:
        cmd = [
            "yt-dlp",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            yt_url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=14)
        if proc.returncode == 0 and proc.stdout:
            info = json.loads(proc.stdout)
            duration = int(info.get("duration") or 1800)
            max_start = max(60, duration - 30)

            for ch in info.get("chapters") or []:
                st = ch.get("start_time")
                title = str(ch.get("title") or "").strip()
                if isinstance(st, (int, float)) and 10 <= int(st) <= max_start:
                    score = 80
                    if re.search(r"(twist|climax|reveal|ending|best|funny|crazy|shock|fail|win|moment|highlight)", title, re.I):
                        score += 20
                    candidates.append({"sec": int(st), "score": score, "reason": title or "Chapter highlight"})

            for hm in info.get("heatmap") or []:
                st = hm.get("start_time")
                val = hm.get("value")
                if isinstance(st, (int, float)) and 10 <= int(st) <= max_start:
                    score = int(70 + min(30, float(val or 0) * 30))
                    candidates.append({"sec": int(st), "score": score, "reason": "High replay peak"})

            desc = str(info.get("description") or "")
            for m in re.finditer(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—:|]?\s*([^\n]{0,50})", desc):
                sec = parse_sec(m.group(1))
                label = (m.group(2) or "").strip()
                if sec is not None and 10 <= sec <= max_start:
                    score = 75
                    if re.search(r"(twist|climax|reveal|best|funny|crazy|shock|moment)", label, re.I):
                        score += 15
                    candidates.append({"sec": sec, "score": score, "reason": label or "Description highlight"})
    except Exception:
        pass

    # Livestream chat spikes
    candidates.extend(fetch_live_chat_candidates(yt_url, max_start))
    return max_start, candidates


def pick_best_moments(yt_url: str, count: int, dur: int, max_start_arg: int, avoid_list: list[int]):
    max_start, candidates = fetch_video_moment_candidates(yt_url)
    if max_start_arg:
        max_start = max(60, min(max_start, max_start_arg))

    picked = []
    clips = []

    ranked = sorted(
        candidates,
        key=lambda c: c["score"] + _sys_rng.randint(-12, 12),
        reverse=True,
    )

    for cand in ranked:
        if len(clips) >= count:
            break
        s = max(10, min(max_start, int(cand["sec"])))
        if any(abs(s - u) < dur + 20 for u in picked):
            continue
        if any(abs(s - a) < max(15, dur // 2) for a in avoid_list):
            continue
        picked.append(s)
        clips.append({
            "index": len(clips) + 1,
            "start": fmt_hms(s),
            "end": fmt_hms(s + dur),
            "duration": dur,
            "reason": cand["reason"],
        })

    zones = [
        (max(15, int(max_start * 0.10)), max(30, int(max_start * 0.35)), "Early hook moment"),
        (max(30, int(max_start * 0.35)), max(60, int(max_start * 0.72)), "Mid-video peak moment"),
        (max(45, int(max_start * 0.72)), max(60, max_start), "Climax / late highlight"),
    ]

    while len(clips) < count:
        z_low, z_high, z_reason = _sys_rng.choice(zones)
        if z_high <= z_low:
            z_low, z_high = 15, max(60, max_start)
        s = _sys_rng.randint(z_low, z_high)
        tries = 0
        while (
            any(abs(s - u) < dur + 20 for u in picked)
            or any(abs(s - a) < max(15, dur // 2) for a in avoid_list)
        ) and tries < 80:
            s = _sys_rng.randint(15, max(60, max_start))
            tries += 1
        picked.append(s)
        clips.append({
            "index": len(clips) + 1,
            "start": fmt_hms(s),
            "end": fmt_hms(s + dur),
            "duration": dur,
            "reason": z_reason,
        })

    return picked, clips


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
    yt_url = (data.get("url") or data.get("video_url") or "").strip()
    count = max(1, min(10, int(data.get("count") or data.get("num_clips") or 3)))
    dur = max(10, min(180, int(data.get("duration") or data.get("clip_duration") or 25)))
    max_start_arg = int(data.get("max_start") or 0)

    extra_avoid = []
    for item in (data.get("previous_clips") or data.get("exclude") or []):
        s = parse_sec(item.get("start") if isinstance(item, dict) else item)
        if s is not None:
            extra_avoid.append(s)

    with RECENT_LOCK:
        avoid = list(RECENT_STARTS[-50:]) + extra_avoid

    picked, clips = pick_best_moments(yt_url, count, dur, max_start_arg, avoid)

    with RECENT_LOCK:
        RECENT_STARTS.extend(picked)
        if len(RECENT_STARTS) > 120:
            del RECENT_STARTS[:-120]

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
