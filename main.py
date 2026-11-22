import os
import re
import json
import uuid
import base64
import tempfile
import subprocess
import urllib.request
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "clips")
COOKIE_FILE = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEFAULT_GEMINI_KEY = "AQ.Ab8RN6J-6EtD0K99jgJwpstXSCXWBHYLAIy9016d1zhTadLeeg"
DEFAULT_CEREBRAS_KEY = "cr-1a5d2c945538d2665a0a1e866a15b6e746494651d414ea0917d59a21a2"
DEFAULT_GROQ_KEY = "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"


def prepare_cookies_file():
    raw_b64 = (os.environ.get("YT_COOKIES_B64") or "").strip()
    raw_txt = (os.environ.get("YT_COOKIES_TXT") or "").strip()

    if raw_b64:
        try:
            with open(COOKIE_FILE, "wb") as f:
                f.write(base64.b64decode(raw_b64))
            if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
                return COOKIE_FILE
        except Exception:
            pass

    if raw_txt:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(raw_txt)
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
            return COOKIE_FILE

    return COOKIE_FILE if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0 else None


def get_proxy():
    return (os.environ.get("YT_PROXY") or os.environ.get("HTTPS_PROXY") or "").strip() or None


def to_seconds(t_str):
    parts = str(t_str).strip().split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return 0.0


def to_hms(sec):
    sec = max(0, int(round(sec)))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def extract_video_info(url):
    cookie_path = prepare_cookies_file()
    proxy = get_proxy()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path
    if proxy:
        ydl_opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title") or "YouTube Gameplay",
                "duration": float(info.get("duration") or 180),
                "description": (info.get("description") or "")[:2000],
                "chapters": info.get("chapters") or [],
            }
    except Exception:
        return {
            "title": "YouTube Gameplay",
            "duration": 180.0,
            "description": "",
            "chapters": [],
        }


def build_ffmpeg_cmd(raw_path, final_path, aspect="split", cam_side="bottom_right"):
    if aspect == "split":
        pos_map = {
            "bottom_right": ("iw*0.68", "ih*0.65"),
            "bottom_left":  ("0",       "ih*0.65"),
            "top_right":    ("iw*0.68", "0"),
            "top_left":     ("0",       "0"),
        }
        cx, cy = pos_map.get(cam_side, pos_map["bottom_right"])

        filter_complex = (
            f"[0:v]crop=iw*0.32:ih*0.35:{cx}:{cy},scale=720:420:force_original_aspect_ratio=increase,"
            f"crop=720:420,eq=contrast=1.06:saturation=1.08[top];"
            f"[0:v]scale=720:860:force_original_aspect_ratio=increase,"
            f"crop=720:860,eq=contrast=1.05:saturation=1.10[bot];"
            f"[top][bot]vstack=inputs=2,"
            f"drawbox=x=0:y=418:w=720:h=4:color=0xf59e0b@0.95:t=fill[outv]"
        )
        return [
            "ffmpeg", "-y", "-i", raw_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            final_path,
        ]

    if aspect == "9:16":
        vf = "crop=ih*9/16:ih,scale=720:1280,eq=contrast=1.05:saturation=1.08"
    else:
        vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2"

    return [
        "ffmpeg", "-y", "-i", raw_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        final_path,
    ]


def run_yt_dlp_clip(url, start_str, end_str, raw_path, final_path, aspect="split", cam_side="bottom_right"):
    cookie_path = prepare_cookies_file()
    proxy = get_proxy()
    dl_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--sleep-requests", "1",
        "--download-sections", f"*{start_str}-{end_str}",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo*+bestaudio*/best",
        "--merge-output-format", "mp4",
        "--force-keyframes-at-cuts",
        "-o", raw_path,
    ]
    if cookie_path:
        dl_cmd.extend(["--cookies", cookie_path])
    if proxy:
        dl_cmd.extend(["--proxy", proxy])
    dl_cmd.append(url)

    subprocess.run(dl_cmd, check=True, timeout=180)

    if not os.path.exists(raw_path):
        for fname in os.listdir(OUTPUT_DIR):
            base = os.path.splitext(os.path.basename(raw_path))[0]
            if fname.startswith(base) and fname.endswith(".mp4"):
                raw_path = os.path.join(OUTPUT_DIR, fname)
                break

    cmd = build_ffmpeg_cmd(raw_path, final_path, aspect=aspect, cam_side=cam_side)
    subprocess.run(cmd, check=True, timeout=180)


def fallback_moments(duration, max_clips, clip_len):
    safe_dur = max(float(duration or 120), float(clip_len) + 10)
    usable_end = max(float(clip_len), safe_dur - 5)
    if usable_end <= clip_len:
        return [{"start": 0, "end": min(clip_len, safe_dur), "reason": "Best Highlight Segment"}]
    step = max(float(clip_len), (usable_end - clip_len) / max(1, max_clips - 1))
    return [
        {
            "start": round(min(usable_end - clip_len, max(0.0, i * step)), 1),
            "end": round(min(safe_dur, min(usable_end - clip_len, max(0.0, i * step)) + clip_len), 1),
            "reason": f"Gemini AI Highlight #{i + 1}",
        }
        for i in range(max_clips)
    ]


def get_gemini_api_key(request_key=""):
    key = (request_key or os.environ.get("GEMINI_API_KEY") or DEFAULT_GEMINI_KEY or "").strip()
    return key if key else None


def parse_moments_json(txt, duration, max_clips, clip_len, default_label="Gemini AI Moment"):
    if not txt:
        return None
    match = re.search(r"\[[\s\S]*\]", txt)
    if not match:
        return None
    arr = json.loads(match.group(0))
    valid = []
    for item in arr[:max_clips]:
        st = max(0.0, float(item.get("start", 0)))
        en = min(float(duration), max(st + 5.0, float(item.get("end", st + clip_len))))
        if en > st:
            valid.append({
                "start": st,
                "end": en,
                "reason": str(item.get("reason") or default_label)[:90],
            })
    return valid if valid else None


def build_prompt(info, max_clips, clip_len):
    duration = float(info.get("duration") or 120)
    return (
        f"Analyze this video for viral short clips.\n"
        f"Title: {info.get('title')}\n"
        f"Duration (seconds): {duration}\n"
        f"Description: {info.get('description')}\n"
        f"Pick {max_clips} best exciting moments of around {clip_len} seconds each within 0 to {duration}.\n"
        f"Return ONLY a JSON array of objects with keys: start (number in seconds), end (number in seconds), reason (short reason)."
    )


def pick_gemini_moments(info, max_clips, clip_len, gemini_key=""):
    duration = float(info.get("duration") or 120)
    api_key = get_gemini_api_key(gemini_key)
    if not api_key:
        return None

    prompt = build_prompt(info, max_clips, clip_len)
    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash"]:
        try:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}]
            }).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                txt = raw["candidates"][0]["content"]["parts"][0]["text"]
                parsed = parse_moments_json(txt, duration, max_clips, clip_len, "Gemini AI Moment")
                if parsed:
                    return parsed
        except Exception:
            continue
    return None


def analyze_gemini_only(info, max_clips, clip_len, gemini_key=""):
    duration = float(info.get("duration") or 120)
    moments = pick_gemini_moments(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key)
    if moments:
        return moments, "gemini"

    chapters = info.get("chapters") or []
    if chapters:
        picked = []
        for ch in chapters[:max_clips]:
            st = float(ch.get("start_time") or 0)
            en = min(duration, st + float(clip_len))
            if en > st:
                picked.append({"start": st, "end": en, "reason": ch.get("title") or "Chapter Moment"})
        if picked:
            return picked, "chapters"

    return fallback_moments(duration, max_clips, clip_len), "fallback"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "clipcutter-api",
        "gemini_ready": bool(get_gemini_api_key()),
        "cookies_active": bool(prepare_cookies_file()),
    }), 200


@app.route("/analyze-moments", methods=["POST"])
def analyze_moments():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    gemini_key = (data.get("gemini_api_key") or "").strip()
    max_clips = max(1, min(5, int(data.get("max_clips") or 3)))
    clip_len = max(10, min(60, int(data.get("clip_len") or 25)))
    if not url:
        return jsonify({"error": "YouTube URL required"}), 400

    info = extract_video_info(url)
    moments, source = analyze_gemini_only(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key)
    formatted = [
        {
            "start": to_hms(m["start"]),
            "end": to_hms(m["end"]),
            "start_sec": m["start"],
            "end_sec": m["end"],
            "reason": m.get("reason") or "Gemini AI Moment",
        }
        for m in moments
    ]
    return jsonify({
        "title": info.get("title"),
        "duration": info.get("duration"),
        "source": source,
        "moments": formatted,
    }), 200


@app.route("/cut", methods=["POST"])
def cut_video():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    start_str = (data.get("start") or "00:00:00").strip()
    end_str = (data.get("end") or "00:00:15").strip()
    aspect = (data.get("aspect") or "split").strip()
    cam_side = (data.get("cam_side") or "bottom_right").strip()
    if not url:
        return jsonify({"error": "YouTube URL required"}), 400
    start_sec = to_seconds(start_str)
    end_sec = to_seconds(end_str)
    if end_sec <= start_sec:
        return jsonify({"error": "End time must be greater than start time"}), 400

    job_id = str(uuid.uuid4())[:8]
    raw_path = os.path.join(OUTPUT_DIR, f"raw_{job_id}.mp4")
    final_name = f"cut_{job_id}.mp4"
    final_path = os.path.join(OUTPUT_DIR, final_name)
    try:
        run_yt_dlp_clip(url, to_hms(start_sec), to_hms(end_sec), raw_path, final_path, aspect=aspect, cam_side=cam_side)
        if not os.path.exists(final_path):
            return jsonify({"error": "Output clip not generated"}), 500
        return send_from_directory(OUTPUT_DIR, final_name, as_attachment=True, download_name=final_name)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Processing timeout"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass


@app.route("/auto-cut", methods=["POST"])
def auto_cut():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    aspect = (data.get("aspect") or "split").strip()
    cam_side = (data.get("cam_side") or "bottom_right").strip()
    gemini_key = (data.get("gemini_api_key") or "").strip()
    max_clips = max(1, min(5, int(data.get("max_clips") or 3)))
    clip_len = max(10, min(60, int(data.get("clip_len") or 25)))
    if not url:
        return jsonify({"error": "YouTube URL required"}), 400

    info = extract_video_info(url)
    moments, _ = analyze_gemini_only(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key)
    results = []
    for idx, m in enumerate(moments, start=1):
        st_hms = to_hms(m["start"])
        en_hms = to_hms(m["end"])
        job_id = f"{uuid.uuid4().hex[:6]}_{idx}"
        raw_path = os.path.join(OUTPUT_DIR, f"auto_raw_{job_id}.mp4")
        out_name = f"auto_clip_{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        try:
            run_yt_dlp_clip(url, st_hms, en_hms, raw_path, out_path, aspect=aspect, cam_side=cam_side)
            if os.path.exists(out_path):
                results.append({
                    "start": st_hms,
                    "end": en_hms,
                    "reason": m.get("reason") or "Gemini AI Moment",
                    "download_url": f"/download/{out_name}",
                })
            else:
                results.append({"start": st_hms, "end": en_hms, "error": "Clip file missing"})
        except Exception as clip_err:
            results.append({"start": st_hms, "end": en_hms, "error": str(clip_err)})
        finally:
            if os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
    return jsonify({"title": info.get("title"), "clips": results}), 200


@app.route("/download/<filename>", methods=["GET"])
def download_file(filename):
    safe_name = os.path.basename(filename)
    full_path = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.exists(full_path):
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=True, download_name=safe_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
