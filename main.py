import json
import os
import re
import subprocess
import tempfile
import urllib.request
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.expanduser("~/clipcutter")
OUT_DIR = "/sdcard/Movies/ClipCutter"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def fmt_sec(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_sec(val) -> int:
    if isinstance(val, (int, float)):
        return max(0, int(val))
    parts = [int(float(x)) for x in str(val).strip().split(":") if x.strip() != ""]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def safe_name(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", s)


def fetch_ytdlp_meta(url: str) -> dict:
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-warnings",
        "--no-playlist",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"title": "", "duration": 600, "chapters": [], "description": ""}
    try:
        info = json.loads(proc.stdout.splitlines()[-1])
    except Exception:
        return {"title": "", "duration": 600, "chapters": [], "description": ""}

    duration = int(info.get("duration") or 600)
    chapters = []
    for ch in info.get("chapters") or []:
        st = int(ch.get("start_time") or 0)
        en = int(ch.get("end_time") or (st + 25))
        chapters.append({
            "title": ch.get("title") or "",
            "start": fmt_sec(st),
            "end": fmt_sec(en),
            "start_sec": st,
        })

    return {
        "title": info.get("title") or "",
        "duration": max(30, duration),
        "chapters": chapters,
        "description": (info.get("description") or "")[:1200],
    }


def ask_gemini_moments(meta: dict, count: int, clip_len: int, exclude: list, seed: int) -> list:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return []

    prompt = (
        f"You are a viral YouTube Shorts & gaming highlight analyzer.\n"
        f"Video Title: {meta.get('title', '')}\n"
        f"Duration (sec): {meta.get('duration', 600)}\n"
        f"Chapters: {json.dumps(meta.get('chapters', [])[:15])}\n"
        f"Description snippet: {meta.get('description', '')}\n"
        f"Exclude ranges: {json.dumps(exclude[-30:])}\n"
        f"Seed: {seed}\n\n"
        f"Pick {count} distinct, high-retention highlight segments of ~{clip_len} seconds each "
        f"within 0 to {meta.get('duration', 600)} seconds. Do NOT repeat any excluded range.\n"
        f'Return ONLY valid JSON array like: [{{"start":"00:01:10","end":"00:01:35"}}]'
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9},
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    text = ""
    for cand in body.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        for p in parts:
            if p.get("text"):
                text += p["text"]

    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []

    raw_list = json.loads(match.group(0))
    duration = int(meta.get("duration") or 600)
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        st = max(0, min(max(0, duration - clip_len), to_sec(item.get("start", 0))))
        en = min(duration, st + max(5, clip_len))
        out.append({"start": fmt_sec(st), "end": fmt_sec(en)})
    return out


def build_distinct_moments(meta: dict, gemini_list: list, count: int, clip_len: int, exclude: list, seed: int) -> list:
    duration = max(clip_len + 10, int(meta.get("duration") or 600))
    excluded_set = set(str(x).strip() for x in (exclude or []))
    chosen = []

    pool = list(gemini_list or [])
    for ch in meta.get("chapters") or []:
        st = max(0, min(duration - clip_len, int(ch.get("start_sec") or 0) + (seed % 11)))
        pool.append({"start": fmt_sec(st), "end": fmt_sec(min(duration, st + clip_len))})

    for m in pool:
        if len(chosen) >= count:
            break
        st = max(0, min(duration - clip_len, to_sec(m.get("start", 0))))
        en = min(duration, st + clip_len)
        key = f"{fmt_sec(st)}-{fmt_sec(en)}"
        overlap = any(st < to_sec(c["end"]) + 4 and en + 4 > to_sec(c["start"]) for c in chosen)
        if key not in excluded_set and not overlap:
            chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})
            excluded_set.add(key)

    guard = 0
    while len(chosen) < count and guard < 150:
        guard += 1
        step = 15 + ((seed + guard * 19 + len(chosen) * 29) % 53)
        st = (seed + guard * step) % max(1, duration - clip_len)
        en = min(duration, st + clip_len)
        key = f"{fmt_sec(st)}-{fmt_sec(en)}"
        overlap = any(st < to_sec(c["end"]) + 4 and en + 4 > to_sec(c["start"]) for c in chosen)
        if key not in excluded_set and not overlap:
            chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})
            excluded_set.add(key)

    while len(chosen) < count:
        idx = len(chosen)
        st = (seed + idx * (clip_len + 17)) % max(1, duration - clip_len)
        en = min(duration, st + clip_len)
        chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})

    return chosen[:count]


def build_vf(aspect: str, cam_side: str) -> str:
    if aspect == "16:9":
        return "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

    if aspect == "9:16":
        return "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,format=yuv420p"

    cam_x = "iw*0.55" if "right" in cam_side else "0"
    cam_y = "ih*0.55" if "bottom" in cam_side else "0"
    return (
        f"[0:v]scale=1280:720[base];"
        f"[base]crop=iw*0.45:ih*0.45:{cam_x}:{cam_y},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[top];"
        f"[base]scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[bot];"
        f"[top][bot]vstack=inputs=2,format=yuv420p[v]"
    )


def cut_with_ffmpeg(input_file: str, start: str, end: str, aspect: str, cam_side: str, out_file: str):
    vf = build_vf(aspect, cam_side)
    cmd = ["ffmpeg", "-y", "-ss", start, "-to", end, "-i", input_file]

    if aspect == "split":
        cmd += [
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            out_file,
        ]
    else:
        cmd += [
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            out_file,
        ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-400:])


@app.get("/health")
def health():
    return jsonify({"status": "ok", "outdir": OUT_DIR})


@app.post("/analyze-moments")
def analyze_moments():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    count = max(1, min(10, int(data.get("max_clips") or 3)))
    clip_len = max(5, min(180, int(data.get("clip_len") or 25)))
    exclude = data.get("exclude") or []
    seed = int(data.get("seed") or data.get("ts") or 42)

    meta = fetch_ytdlp_meta(url)
    gemini_list = []
    try:
        gemini_list = ask_gemini_moments(meta, count + 2, clip_len, exclude, seed)
    except Exception:
        gemini_list = []

    moments = build_distinct_moments(meta, gemini_list, count, clip_len, exclude, seed)
    return jsonify({
        "title": meta.get("title", ""),
        "duration": meta.get("duration", 600),
        "analyzer": "gemini+ytdlp" if gemini_list else "ytdlp",
        "moments": moments,
    })


@app.post("/cut")
def cut_video():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    start = (data.get("start") or "00:00:00").strip()
    end = (data.get("end") or "00:00:20").strip()
    aspect = (data.get("aspect") or "split").strip()
    cam_side = (data.get("cam_side") or "bottom_right").strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    with tempfile.TemporaryDirectory(dir=BASE_DIR) as tmpdir:
        src_path = os.path.join(tmpdir, "src.mp4")
        fname = f"clip_{safe_name(start)}_{safe_name(end)}_{safe_name(aspect)}.mp4"
        saved_path = os.path.join(OUT_DIR, fname)

        dl_cmd = [
            "yt-dlp",
            "-f",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "--no-playlist",
            "-o",
            src_path,
            url,
        ]
        dl_res = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=180)
        if dl_res.returncode != 0 or not os.path.exists(src_path):
            return jsonify({"error": "yt-dlp download failed", "detail": dl_res.stderr[-400:]}), 500

        try:
            cut_with_ffmpeg(src_path, start, end, aspect, cam_side, saved_path)
        except Exception as e:
            return jsonify({"error": "ffmpeg cut failed", "detail": str(e)}), 500

        if not os.path.exists(saved_path):
            return jsonify({"error": "No output mp4 produced"}), 500

        return send_file(saved_path, mimetype="video/mp4", as_attachment=True, download_name=fname)


@app.post("/trim-local")
def trim_local():
    if "file" not in request.files:
        return jsonify({"error": "Missing file"}), 400
    f = request.files["file"]
    start = float(request.form.get("start") or 0)
    end = float(request.form.get("end") or 10)
    duration = max(0.5, end - start)

    with tempfile.TemporaryDirectory(dir=BASE_DIR) as tmpdir:
        in_path = os.path.join(tmpdir, "in.mp4")
        out_path = os.path.join(tmpdir, "trimmed.mp4")
        f.save(in_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(max(0.0, start)),
            "-i",
            in_path,
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            out_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0 or not os.path.exists(out_path):
            return jsonify({"error": "ffmpeg trim failed"}), 500

        return send_file(out_path, mimetype="video/mp4", as_attachment=True, download_name="local_trimmed.mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 5000)
    app.run(host="127.0.0.1", port=port)
