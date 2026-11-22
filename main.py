import argparse
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.request
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS

try:
    import requests as http_requests
except ImportError:
    http_requests = None

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.expanduser("~/clipcutter")
OUT_DIR = "/sdcard/Movies/ClipCutter"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

RENDER_API_URL = os.environ.get("RENDER_API_URL", "https://clipcutter-api-rm3t.onrender.com").rstrip("/")
TERMUX_API_URL = os.environ.get("TERMUX_API_URL", "").rstrip("/")


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
        f"Exclude ranges: {json.dumps(exclude[-50:])}\n"
        f"Seed: {seed}\n\n"
        f"Pick {count} completely new, distinct highlight segments of ~{clip_len} seconds each "
        f"within 0 to {meta.get('duration', 600)} seconds. Never reuse or overlap any excluded range.\n"
        f'Return ONLY valid JSON array like: [{{"start":"00:01:10","end":"00:01:35"}}]'
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 1.15},
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


def is_overlapping(st: int, en: int, chosen: list, excluded_spans: list) -> bool:
    for c in chosen:
        cst, cen = to_sec(c["start"]), to_sec(c["end"])
        if st < cen + 5 and en + 5 > cst:
            return True
    for est, een in excluded_spans:
        if st < een + 3 and en + 3 > est:
            return True
    return False


def build_distinct_moments(meta: dict, gemini_list: list, count: int, clip_len: int, exclude: list, seed: int) -> list:
    duration = max(clip_len + 15, int(meta.get("duration") or 600))
    max_start = max(1, duration - clip_len)
    excluded_spans = []
    for item in (exclude or []):
        s = str(item).strip()
        if "-" in s:
            left, right = s.split("-", 1)
            excluded_spans.append((to_sec(left), to_sec(right)))

    rng = random.Random(seed)
    chosen = []

    pool = list(gemini_list or [])
    chapters = list(meta.get("chapters") or [])
    if chapters:
        rot = rng.randrange(len(chapters))
        chapters = chapters[rot:] + chapters[:rot]
        for ch in chapters:
            jitter = rng.randint(5, 45)
            st = max(0, min(max_start, int(ch.get("start_sec") or 0) + jitter))
            pool.append({"start": fmt_sec(st), "end": fmt_sec(min(duration, st + clip_len))})

    for m in pool:
        if len(chosen) >= count:
            break
        st = max(0, min(max_start, to_sec(m.get("start", 0))))
        en = min(duration, st + clip_len)
        if not is_overlapping(st, en, chosen, excluded_spans):
            chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})

    attempts = 0
    while len(chosen) < count and attempts < 250:
        attempts += 1
        st = rng.randint(0, max_start)
        en = min(duration, st + clip_len)
        if not is_overlapping(st, en, chosen, excluded_spans):
            chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})

    fallback_tries = 0
    while len(chosen) < count and fallback_tries < 100:
        fallback_tries += 1
        st = rng.randint(0, max_start)
        en = min(duration, st + clip_len)
        if not any(st < to_sec(c["end"]) and en > to_sec(c["start"]) for c in chosen):
            chosen.append({"start": fmt_sec(st), "end": fmt_sec(en)})

    while len(chosen) < count:
        st = (rng.randint(0, max_start) + len(chosen) * (clip_len + 7)) % max(1, max_start + 1)
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


def run_cli_batch(input_file: str, ranges_str: str, aspect: str, cam_side: str, outdir: str):
    os.makedirs(outdir, exist_ok=True)
    ranges = [r.strip() for r in ranges_str.split(",") if r.strip()]
    for idx, rng in enumerate(ranges, start=1):
        parts = [p.strip() for p in rng.split("-")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        st, en = parts[0], parts[1]
        fname = f"clip_{idx}_{safe_name(st)}_{safe_name(en)}_{safe_name(aspect)}.mp4"
        out_path = os.path.join(outdir, fname)
        cut_with_ffmpeg(input_file, st, en, aspect, cam_side, out_path)
        print(f"SAVED: {out_path}")


def forward_request(target_base: str, path: str, timeout: int = 120):
    if http_requests is None:
        raise RuntimeError("requests package is not installed")
    url = f"{target_base}{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    resp = http_requests.request(
        method=request.method,
        url=url,
        params=request.args,
        data=request.files if request.files else request.get_data(),
        headers=None if request.files else headers,
        timeout=timeout,
        stream=True,
    )
    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]
    return Response(resp.iter_content(chunk_size=65536), status=resp.status_code, headers=out_headers)


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "outdir": OUT_DIR,
        "render": RENDER_API_URL,
        "termux": TERMUX_API_URL,
    })


@app.get("/gateway-status")
def gateway_status():
    return jsonify({
        "status": "ok",
        "render": RENDER_API_URL,
        "termux": TERMUX_API_URL,
    })


@app.post("/set-termux")
def set_termux():
    global TERMUX_API_URL
    data = request.get_json(silent=True) or {}
    new_url = (data.get("url") or "").strip().rstrip("/")
    if not new_url:
        return jsonify({"error": "Missing url"}), 400
    TERMUX_API_URL = new_url
    return jsonify({"status": "updated", "termux": TERMUX_API_URL, "render": RENDER_API_URL})


@app.route("/route/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def route_gateway(subpath: str):
    path = f"/{subpath}"
    is_analyze = "analyze" in path

    order = [RENDER_API_URL, TERMUX_API_URL] if is_analyze else [TERMUX_API_URL, RENDER_API_URL]
    order = [u for i, u in enumerate(order) if u and u not in order[:i]]

    last_err = None
    for base in order:
        try:
            return forward_request(base, path, timeout=35 if is_analyze else 120)
        except Exception as e:
            last_err = str(e)

    return jsonify({"error": "All upstream APIs failed", "detail": last_err}), 502


@app.post("/analyze-moments")
def analyze_moments():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    count = max(1, min(10, int(data.get("max_clips") or 3)))
    clip_len = max(5, min(180, int(data.get("clip_len") or 25)))
    exclude = data.get("exclude") or []
    raw_seed = data.get("seed") if data.get("seed") is not None else data.get("ts")
    seed = int(raw_seed) if raw_seed is not None else int(time.time() * 1000) ^ random.randint(1, 1_000_000)

    meta = fetch_ytdlp_meta(url)
    gemini_list = []
    try:
        gemini_list = ask_gemini_moments(meta, count + 3, clip_len, exclude, seed)
    except Exception:
        gemini_list = []

    moments = build_distinct_moments(meta, gemini_list, count, clip_len, exclude, seed)
    return jsonify({
        "title": meta.get("title", ""),
        "duration": meta.get("duration", 600),
        "analyzer": "gemini+ytdlp" if gemini_list else "ytdlp",
        "seed": seed,
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


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input")
    parser.add_argument("--ranges")
    parser.add_argument("--aspect", default="split")
    parser.add_argument("--cam-side", default="bottom_right")
    parser.add_argument("--outdir", default=OUT_DIR)
    args, _ = parser.parse_known_args()
    return args


if __name__ == "__main__":
    cli_args = parse_args()
    if cli_args.input and cli_args.ranges:
        run_cli_batch(cli_args.input, cli_args.ranges, cli_args.aspect, cli_args.cam_side, cli_args.outdir)
    else:
        port = int(os.environ.get("PORT") or 5000)
        app.run(host="127.0.0.1", port=port)
