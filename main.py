import os
import re
import json
import uuid
import tempfile
import subprocess
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "clips")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEFAULT_GEMINI_KEY = "AQ.Ab8RN6J-6EtD0K99jgJwpstXSCXWBHYLAIy9016d1zhTadLeeg"


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
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web", "tv"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title") or "YouTube Video",
            "duration": float(info.get("duration") or 120),
            "description": (info.get("description") or "")[:2000],
            "chapters": info.get("chapters") or [],
        }


def run_yt_dlp_clip(url, start_str, end_str, raw_path, final_path, aspect="9:16"):
    dl_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--extractor-args", "youtube:player_client=android,web,tv",
        "--download-sections", f"*{start_str}-{end_str}",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo*+bestaudio*/best",
        "--merge-output-format", "mp4",
        "--force-keyframes-at-cuts",
        "-o", raw_path,
        url,
    ]
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


def pick_gemini_moments(info, max_clips, clip_len, gemini_key=""):
    duration = float(info.get("duration") or 120)
    api_key = get_gemini_api_key(gemini_key)
    if not api_key:
        return None

    prompt = (
        f"Video title: {info.get('title')}\n"
        f"Duration (seconds): {duration}\n"
        f"Description: {info.get('description')}\n"
        f"Pick {max_clips} engaging viral clips of {clip_len} seconds each. "
        f"Return ONLY JSON array of objects with keys start (seconds number), end (seconds number), reason (short string)."
    )

    try:
        import urllib.request
        for model_name in ["gemini-2.5-flash", "gemini-2.0-flash"]:
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
                match = re.search(r"\[[\s\S]*\]", txt)
                if match:
                    arr = json.loads(match.group(0))
                    valid = []
                    for item in arr[:max_clips]:
                        st = max(0.0, float(item.get("start", 0)))
                        en = min(duration, max(st + 5.0, float(item.get("end", st + clip_len))))
                        if en > st:
                            valid.append({
                                "start": st,
                                "end": en,
                                "reason": str(item.get("reason") or "Gemini Highlight")[:90],
                            })
                    if valid:
                        return valid
    except Exception:
        pass

    return None


def pick_ai_moments(info, max_clips, clip_len, ai_engine="gemini", gemini_key=""):
    duration = info.get("duration", 120)
    gemini_moments = pick_gemini_moments(info, max_clips=max_clips, clip_len=clip_len, gemini_key=gemini_key)
    if gemini_moments:
        return gemini_moments

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
        "service": "clip
