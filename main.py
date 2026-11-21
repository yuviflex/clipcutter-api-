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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title") or "YouTube Video",
            "duration": float(info.get("duration") or 120),
            "description": (info.get("description") or "")[:2000],
            "chapters": info.get("chapters") or [],
        }


def run_yt_dlp_clip(url, start_str, end_str, raw_path, final_path, aspect="9:16"):
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

    if aspect == "9:16":
        cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", "crop=ih*9/16:ih,scale=720:1280",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            final_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
            "-c:a", "aac", "-b:a", "128k",
            final_path,
        ]
    subprocess.run(cmd, check=True, timeout=180)


def fallback_moments(duration, max_clips, clip_len):
    safe_dur = max(float(duration or 90), float(clip_len) + 5)
    usable_end = max(float(clip_len), safe_dur - 5)
    if usable_end <= clip_len:
        return [{"start": 0, "end": min(clip_len, safe_dur), "reason": "Best Highlight Segment"}]
    step = max(float(clip_len), (usable_end - clip_len) / max(1, max_clips - 1))
    return [
        {
            "start": round(min(usable_end - clip_len, max(0.0, i * step)), 1),
            "end": round(min(safe_dur, min(usable_end - clip_len, max(0.0, i * step)) + clip_len), 1),
            "reason": f"High-energy highlight #{i + 1}",
        }
        for i in range(max_clips)
    ]


def get_gemini_api_key(request_key=""):
    key = (request_key or os.environ.get("GEMINI_API_KEY") or DEFAULT_GEMINI_KEY or "").strip()
    return key if key else None


def get_groq_api_key(request_key=""):
    key = (request_key or os.environ.get("GROQ_API_KEY") or DEFAULT_GROQ_KEY or "").strip()
    return key if key else None


def parse_moments_json(txt, duration, max_clips, clip_len, default_label):
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
        f"Video title: {info.get('title')}\n"
        f"Duration (seconds): {duration}\n"
        f"Description: {info.get('description')}\n"
        f"Pick {max_clips} engaging viral clips of {clip_len} seconds each. "
        f"Return ONLY JSON array of objects with keys start (seconds number), end (seconds number), reason (short string)."
    )


def pick_groq_moments(info, max_clips, clip_len, groq_key=""):
    duration = float(info.get("duration") or 120)
    api_key = get_groq_api_key(groq_key)
    if not api_key:
        return None

    prompt = build_prompt(info, max_clips, clip_len)
    models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    for model_name in models:
        try:
            payload = json.dumps({
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "Return ONLY a valid JSON array."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                txt = raw["choices"][0]["message"]["content"]
                parsed = parse_moments_json(txt, duration, max_clips, clip_len, "Groq Highlight")
                if parsed:
                    return parsed
        except Exception:
            continue
    return None


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
                parsed = parse_moments_json(txt, duration, max_clips, clip_len, "Gemini Highlight")
                if parsed:
                    return parsed
        except Exception:
            continue
    return None


def pick_ai_moments(info, max_clips, clip_len, ai_engine="multi", gemini_key="", groq_key=""):
    duration = info.get("duration", 120)
    engine = (ai_engine or "multi").lower()

    if engine == "gemini":
        order = [
            pick_gemini_moments(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key),
            pick_groq_moments(info, max_clips=max_clips, clip_len=clip_len, groq_key=groq_key),
        ]
    elif engine == "groq":
        order = [
            pick_groq_moments(info, max_clips=max_clips, clip_len=clip_len, groq_key=groq_key),
            pick_gemini_moments(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key),
        ]
    else:
        order = [
            pick_groq_moments(info, max_clips=max_clips, clip_len=clip_len, groq_key=groq_key),
            pick_gemini_moments(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key),
        ]

    for moments in order:
        if moments:
            return moments

    chapters = info.get("chapters") or []
    if chapters:
        picked = []
        for ch in chapters[:max_clips]:
            st = float(ch.get("start_time") or 0)
            en = min(float(duration), st + float(clip_len))
            if en > st:
                picked.append({"start": st, "end": en, "reason": ch.get("title") or "Chapter Highlight"})
        if picked:
            return picked

    return fallback_moments(duration, max_clips, clip_len)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "clipcutter-api",
        "gemini_ready": bool(get_gemini_api_key()),
        "groq_ready": bool(get_groq_api_key()),
        "cookies_active": bool(prepare_cookies_file()),
    }), 200


@app.route("/cut", methods=["POST"])
def cut_video():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    start_str = (data.get("start") or "00:00:00").strip()
    end_str = (data.get("end") or "00:00:15").strip()
    aspect = (data.get("aspect") or "9:16").strip()
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
        run_yt_dlp_clip(url, to_hms(start_sec), to_hms(end_sec), raw_path, final_path, aspect=aspect)
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
    aspect = (data.get("aspect") or "9:16").strip()
    ai_engine = (data.get("ai_engine") or "multi").strip()
    gemini_key = (data.get("gemini_api_key") or "").strip()
    groq_key = (data.get("groq_api_key") or "").strip()
    max_clips = max(1, min(5, int(data.get("max_clips") or 3)))
    clip_len = max(10, min(60, int(data.get("clip_len") or 25)))
    if not url:
        return jsonify({"error": "YouTube URL required"}), 400

    try:
        info = extract_video_info(url)
        moments = pick_ai_moments(
            info,
            max_clips=max_clips,
            clip_len=clip_len,
            ai_engine=ai_engine,
            gemini_key=gemini_key,
            groq_key=groq_key,
        )
        results = []
        for idx, m in enumerate(moments, start=1):
            st_hms = to_hms(m["start"])
            en_hms = to_hms(m["end"])
            job_id = f"{uuid.uuid4().hex[:6]}_{idx}"
            raw_path = os.path.join(OUTPUT_DIR, f"auto_raw_{job_id}.mp4")
            out_name = f"auto_clip_{job_id}.mp4"
            out_path = os.path.join(OUTPUT_DIR, out_name)
            try:
                run_yt_dlp_clip(url, st_hms, en_hms, raw_path, out_path, aspect=aspect)
                if os.path.exists(out_path):
                    results.append({
                        "start": st_hms,
                        "end": en_hms,
                        "reason": m.get("reason") or "AI Highlight",
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
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
