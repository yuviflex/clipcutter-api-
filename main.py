import os
import re
import json
import uuid
import subprocess
import urllib.request
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = "/tmp/clip_jobs"
os.makedirs(BASE_DIR, exist_ok=True)


def parse_timestamp(ts_str):
    parts = [int(float(x)) for x in str(ts_str).strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return int(parts[0])


def format_timestamp(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def run_yt_dlp_clip(url, start_str, end_str, raw_path, final_path, aspect="9:16"):
    dl_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--download-sections", f"*{start_str}-{end_str}",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", raw_path,
        url
    ]
    subprocess.run(dl_cmd, check=True, timeout=120)

    if aspect == "9:16":
        crop_cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", "crop=ih*9/16:ih,scale=720:1280",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
            "-c:a", "aac",
            final_path
        ]
        subprocess.run(crop_cmd, check=True, timeout=120)
    else:
        os.replace(raw_path, final_path)


def fetch_video_meta_and_subs(url, job_dir):
    title = ""
    duration = 600
    sub_text = ""

    try:
        info_cmd = ["yt-dlp", "--no-playlist", "--print", "%(title)s\n%(duration)s", url]
        res = subprocess.run(info_cmd, capture_output=True, text=True, timeout=35)
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if len(lines) >= 1:
            title = lines[0]
        if len(lines) >= 2:
            try:
                duration = int(float(lines[1]))
            except Exception:
                duration = 600
    except Exception:
        pass

    try:
        sub_cmd = [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--write-auto-subs",
            "--sub-lang", "en,hi",
            "--sub-format", "vtt",
            "-o", os.path.join(job_dir, "sub_%(id)s.%(ext)s"),
            url
        ]
        subprocess.run(sub_cmd, check=False, timeout=45)
        vtt_files = [f for f in os.listdir(job_dir) if f.endswith(".vtt")]
        chunks = []
        for vf in vtt_files:
            with open(os.path.join(job_dir, vf), "r", encoding="utf-8", errors="ignore") as f:
                raw = re.sub(r"<[^>]+>", " ", f.read())
                raw = re.sub(r"WEBVTT.*?\n\n", "", raw, flags=re.S)
                chunks.append(raw[:6000])
        sub_text = "\n".join(chunks)[:8000]
    except Exception:
        pass

    return title, duration, sub_text


def ask_gemini_for_moments(api_key, title, duration, sub_text, max_clips, clip_len):
    if not api_key:
        return None
    prompt = (
        f"You are a viral YouTube Shorts editor. Video title: '{title}', total duration: {duration}s.\n"
        f"Transcript/captions snippet:\n{sub_text or 'No captions available'}\n\n"
        f"Pick {max_clips} viral highlight start times in seconds (between 0 and {max(10, duration - clip_len)}) "
        f"and a short reason for each. Reply strictly as a JSON list of objects with keys "
        f"'start_sec' (integer) and 'reason' (short string). No markdown."
    )
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")

    auth_variants = [
        {"headers": {"Content-Type": "application/json", "x-goog-api-key": api_key}, "suffix": ""},
        {"headers": {"Content-Type": "application/json"}, "suffix": f"?key={api_key}"},
        {"headers": {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, "suffix": ""},
    ]

    for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
        base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for variant in auth_variants:
            endpoint = f"{base_url}{variant['suffix']}"
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers=variant["headers"],
                method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    text = body["candidates"][0]["content"]["parts"][0]["text"]
                    match = re.search(r"\[.*\]", text, re.S)
                    if not match:
                        continue
                    parsed = json.loads(match.group(0))
                    out = []
                    for item in parsed[:max_clips]:
                        st = max(0, min(max(0, duration - clip_len), int(item.get("start_sec", 0))))
                        out.append({
                            "start_sec": st,
                            "end_sec": st + clip_len,
                            "reason": f"[Gemini] {str(item.get('reason', 'Viral Moment'))[:60]}"
                        })
                    if out:
                        return out
            except Exception as e:
                app.logger.warning(f"Gemini attempt failed ({model}): {e}")
                continue
    return None


def ask_claude_for_moments(api_key, title, duration, sub_text, max_clips, clip_len):
    if not api_key:
        return None
    prompt = (
        f"You are a viral YouTube Shorts editor. Video title: '{title}', total duration: {duration}s.\n"
        f"Transcript/captions snippet:\n{sub_text or 'No captions available'}\n\n"
        f"Pick {max_clips} viral highlight start times in seconds (between 0 and {max(10, duration - clip_len)}) "
        f"and a short reason for each. Reply strictly as a JSON list of objects with keys "
        f"'start_sec' (integer) and 'reason' (short string). No markdown."
    )
    endpoint = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        text = body["content"][0]["text"]
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        out = []
        for item in parsed[:max_clips]:
            st = max(0, min(max(0, duration - clip_len), int(item.get("start_sec", 0))))
            out.append({
                "start_sec": st,
                "end_sec": st + clip_len,
                "reason": f"[Claude] {str(item.get('reason', 'Viral Moment'))[:60]}"
            })
        return out or None


def fallback_smart_moments(duration, sub_text, max_clips, clip_len):
    moments = []
    if sub_text:
        keywords = [
            "oh my god", "no way", "crazy", "insane", "final",
            "fight", "police", "chase", "shoot", "let's go", "khatarnak", "gazab"
        ]
        pattern = re.compile(r"(\d{2}:\d{2}:\d{2})\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}\s*\n(.*?)(?=\n\n|\Z)", re.S)
        hits = []
        for m in pattern.finditer(sub_text):
            sec = parse_timestamp(m.group(1))
            clean_txt = re.sub(r"\s+", " ", m.group(2)).strip().lower()
            score = sum(1 for kw in keywords if kw in clean_txt)
            if score > 0:
                hits.append((sec, score, clean_txt[:50]))
        for sec, score, snip in sorted(hits, key=lambda x: -x[1]):
            if all(abs(sec - ex["start_sec"]) >= clip_len + 15 for ex in moments):
                st = max(0, min(max(0, duration - clip_len), sec - 4))
                moments.append({
                    "start_sec": st,
                    "end_sec": st + clip_len,
                    "reason": f"[Multi-AI Smart] {snip or 'High Energy'}"
                })
            if len(moments) >= max_clips:
                break

    if not moments:
        if duration > clip_len * 2:
            step = max(clip_len + 10, duration // (max_clips + 1))
            for i in range(1, max_clips + 1):
                st = min(max(0, duration - clip_len), i * step)
                moments.append({
                    "start_sec": st,
                    "end_sec": st + clip_len,
                    "reason": f"[Multi-AI Highlight #{i}]"
                })
        else:
            moments.append({
                "start_sec": 0,
                "end_sec": min(duration, clip_len),
                "reason": "[Multi-AI Highlight #1]"
            })
    return moments[:max_clips]


def find_best_moments(url, job_dir, ai_engine="multi", gemini_key="", claude_key="", max_clips=3, clip_len=25):
    title, duration, sub_text = fetch_video_meta_and_subs(url, job_dir)
    g_key = (gemini_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    c_key = (claude_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    moments = None
    if ai_engine == "gemini":
        try:
            moments = ask_gemini_for_moments(g_key, title, duration, sub_text, max_clips, clip_len)
        except Exception:
            moments = None
    elif ai_engine == "claude":
        try:
            moments = ask_claude_for_moments(c_key, title, duration, sub_text, max_clips, clip_len)
        except Exception:
            moments = None
    else:
        for fn, key in [(ask_gemini_for_moments, g_key), (ask_claude_for_moments, c_key)]:
            if key:
                try:
                    moments = fn(key, title, duration, sub_text, max_clips, clip_len)
                    if moments:
                        break
                except Exception:
                    continue

    if not moments:
        moments = fallback_smart_moments(duration, sub_text, max_clips, clip_len)

    moments.sort(key=lambda x: x["start_sec"])
    for idx, item in enumerate(moments, start=1):
        item["id"] = f"clip_{idx}"
        item["start"] = format_timestamp(item["start_sec"])
        item["end"] = format_timestamp(item["end_sec"])
    return moments


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "multi-ai-yt-clipper"}), 200


@app.route("/cut", methods=["POST"])
def manual_cut():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    start_raw = (data.get("start") or "").strip()
    end_raw = (data.get("end") or "").strip()
    aspect = (data.get("aspect") or "9:16").strip()

    if not url or not start_raw or not end_raw:
        return jsonify({"error": "url, start, and end are required"}), 400

    start_sec = parse_timestamp(start_raw)
    end_sec = parse_timestamp(end_raw)
    if end_sec <= start_sec:
        return jsonify({"error": "end time must be greater than start time"}), 400

    start_str = format_timestamp(start_sec)
    end_str = format_timestamp(end_sec)

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(BASE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    raw_path = os.path.join(job_dir, f"raw_{job_id}.mp4")
    final_path = os.path.join(job_dir, f"clip_{job_id}.mp4")

    try:
        run_yt_dlp_clip(url, start_str, end_str, raw_path, final_path, aspect=aspect)
        return send_file(
            final_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"clip_{start_str.replace(':', '-')}_to_{end_str.replace(':', '-')}.mp4"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/auto-cut", methods=["POST"])
def auto_cut():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    aspect = (data.get("aspect") or "9:16").strip()
    ai_engine = (data.get("ai_engine") or "multi").strip().lower()
    gemini_key = (data.get("gemini_api_key") or "").strip()
    claude_key = (data.get("claude_api_key") or "").strip()
    max_clips = min(max(int(data.get("max_clips", 3)), 1), 5)
    clip_len = min(max(int(data.get("clip_len", 25)), 10), 60)

    if not url:
        return jsonify({"error": "YouTube URL is required"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(BASE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    moments = find_best_moments(
        url=url,
        job_dir=job_dir,
        ai_engine=ai_engine,
        gemini_key=gemini_key,
        claude_key=claude_key,
        max_clips=max_clips,
        clip_len=clip_len
    )
    results = []

    for m in moments:
        clip_name = f"{job_id}_{m['id']}.mp4"
        raw_path = os.path.join(job_dir, f"raw_{clip_name}")
        final_path = os.path.join(job_dir, clip_name)

        try:
            run_yt_dlp_clip(url, m["start"], m["end"], raw_path, final_path, aspect=aspect)
            if os.path.exists(final_path):
                results.append({
                    "id": m["id"],
                    "start": m["start"],
                    "end": m["end"],
                    "reason": m["reason"],
                    "download_url": f"/download/{job_id}/{clip_name}"
                })
        except Exception as e:
            results.append({
                "id": m["id"],
                "start": m["start"],
                "end": m["end"],
                "reason": m["reason"],
                "error": str(e)
            })

    return jsonify({"job_id": job_id, "ai_engine": ai_engine, "clips": results}), 200


@app.route("/download/<job_id>/<filename>", methods=["GET"])
def download_clip(job_id, filename):
    safe_job = os.path.basename(job_id)
    safe_file = os.path.basename(filename)
    file_path = os.path.join(BASE_DIR, safe_job, safe_file)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, mimetype="video/mp4", as_attachment=True, download_name=safe_file)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
