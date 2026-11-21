import glob
import json
import os
import shutil
import subprocess
import uuid

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE = os.path.expanduser("~/clipcutter")
OUT = os.path.join(BASE, "out")
TMP = os.path.join(BASE, "tmp")
os.makedirs(OUT, exist_ok=True)
os.makedirs(TMP, exist_ok=True)


def setup_cookies():
    # 1) Render Secret File  2) COOKIES_FILE env  3) ~/clipcutter/cookies.txt  4) COOKIES_TXT env
    target = os.path.join(BASE, "cookies_rw.txt")
    for p in (
        os.environ.get("COOKIES_FILE"),
        "/etc/secrets/cookies.txt",
        os.path.join(BASE, "cookies.txt"),
    ):
        if p and os.path.exists(p):
            shutil.copy(p, target)
            return target
    text = os.environ.get("COOKIES_TXT")
    if text:
        with open(target, "w") as f:
            f.write(text)
        return target
    return None


COOKIES = setup_cookies()


@app.after_request
def private_network_header(resp):
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


def to_sec(value):
    parts = [float(p) for p in str(value).strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt(sec):
    sec = int(max(0, sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def run(cmd):
    if cmd[0] == "yt-dlp" and COOKIES:
        cmd = [cmd[0], "--cookies", COOKIES] + cmd[1:]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        lines = [l for l in (p.stderr or p.stdout).strip().splitlines() if l.strip()]
        raise RuntimeError(lines[-1] if lines else "command failed")
    return p.stdout


def cut_clip(url, start_sec, end_sec, aspect):
    job = uuid.uuid4().hex[:10]
    work = os.path.join(TMP, job)
    os.makedirs(work, exist_ok=True)
    try:
        run([
            "yt-dlp", "--no-playlist",
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--download-sections", "*%s-%s" % (fmt(start_sec), fmt(end_sec)),
            "--force-keyframes-at-cuts",
            "--merge-output-format", "mp4",
            "-o", os.path.join(work, "raw.%(ext)s"),
            url,
        ])
        raws = glob.glob(os.path.join(work, "raw.*"))
        if not raws:
            raise RuntimeError("Video download nahi hui")
        out_name = "clip_%s.mp4" % job
        out_path = os.path.join(OUT, out_name)
        cmd = ["ffmpeg", "-y", "-i", raws[0]]
        if aspect == "9:16":
            cmd += ["-vf", "crop='min(iw,ih*9/16)':ih,scale=720:1280,setsar=1"]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            out_path,
        ]
        run(cmd)
        return out_name, out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.route("/")
def home():
    return jsonify({"name": "ClipCutter API", "cookies": bool(COOKIES)})


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/cut", methods=["POST"])
def cut():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url missing"}), 400
    try:
        s = to_sec(data.get("start", "0"))
        e = to_sec(data.get("end", "20"))
        if e <= s:
            return jsonify({"error": "End time Start se bada hona chahiye"}), 400
        _, path = cut_clip(url, s, e, data.get("aspect", "9:16"))
        return send_file(path, mimetype="video/mp4")
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


def pick_moments(info, count, length):
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Video ki duration nahi mili")
    length = min(length, duration)
    chosen = []

    def overlaps(a, b):
        return not (b[1] <= a[0] or b[0] >= a[1])

    def add(center, reason):
        start = max(0.0, center - length / 2)
        end = min(duration, start + length)
        start = max(0.0, end - length)
        seg = (start, end, reason)
        if all(not overlaps(seg, c) for c in chosen):
            chosen.append(seg)

    heat = info.get("heatmap") or []
    engine = "even"
    if heat:
        engine = "most-replayed"
        for h in sorted(heat, key=lambda x: x.get("value", 0), reverse=True):
            if len(chosen) >= count:
                break
            center = (float(h["start_time"]) + float(h["end_time"])) / 2
            add(center, "Most replayed moment")
    if len(chosen) < count:
        for i in range(count):
            if len(chosen) >= count:
                break
            add(duration * (i + 1) / (count + 1), "Video ka important hissa")
    chosen.sort(key=lambda c: c[0])
    return chosen[:count], engine


@app.route("/auto-cut", methods=["POST"])
def auto_cut():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url missing"}), 400
    count = max(1, min(6, int(data.get("max_clips", 3))))
    length = max(5, min(90, int(data.get("clip_len", 25))))
    aspect = data.get("aspect", "9:16")
    try:
        info = json.loads(run(["yt-dlp", "--no-playlist", "-J", url]))
        moments, engine = pick_moments(info, count, length)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

    clips = []
    for start, end, reason in moments:
        item = {"start": fmt(start), "end": fmt(end), "reason": reason}
        try:
            name, _ = cut_clip(url, start, end, aspect)
            item["download_url"] = "/download/" + name
        except Exception as ex:
            item["error"] = str(ex)
        clips.append(item)
    return jsonify({"title": info.get("title", "Video"), "engine": engine, "clips": clips})


@app.route("/download/<path:name>")
def download(name):
    return send_from_directory(OUT, name, mimetype="video/mp4")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, threaded=True)
