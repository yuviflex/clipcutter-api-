import os
import re
import json
import uuid
import subprocess
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

OUTPUT_DIR = "/tmp/clips"
os.makedirs(OUTPUT_DIR, exist_ok=True)


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
        crop_cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", "crop=ih*9/16:ih,scale=720:1280",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            final_path,
        ]
        subprocess.run(crop_cmd, check=True, timeout=180)
    else:
        encode_cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
            "-c:a", "aac", "-b:a", "128k",
            final_path,
        ]
        subprocess.run(encode_cmd, check=True, timeout=180)


def fallback_moments(duration, max_clips, clip_len):
    safe_dur = max(float(duration or 90), float(clip_len) + 5)
    usable_end = max(float(clip_len), safe_dur - 5)
    if usable_end <= clip_len:
        return [{"start": 0, "end": min(clip_len, safe_dur), "reason": "Best Highlight Segment"}]

    step = max(float(clip_len), (usable_end - clip_len) / max(1, max_clips - 1))
    moments = []
    for i in range(max_clips):
        st = min(usable_end - clip_len, max(0.0, i * step))
        en = min(safe_dur, st + clip_len)
        moments.append({
            "start": round(st, 1),
            "end": round(en, 1),
            "reason": f"High-energy highlight #{i + 1}",
        })
    return moments


def pick_ai_moments(info, max_clips, clip_len, ai_engine="multi", gemini_key="", claude_key=""):
    duration = info.get("duration", 120)
    chapters = info.get("chapters") or []
    if chapters and ai_engine in ("multi", "smart"):
        picked = []
        for ch in chapters[:max_clips]:
            st = float(ch.get("start_time") or 0)
            en = min(float(duration), st + float(clip_len))
            if en > st:
                picked.append({
                    "start": st,
                    "end": en,
                    "reason": ch.get("title") or "Chapter Highlight",
                })
        if picked:
            return picked

    g_key = (gemini_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    c_key = (claude_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    prompt = (
        f"Video title: {info.get('title')}\n"
        f"Duration (seconds): {duration}\n"
        f"Description: {info.get('description')}\n"
        f"Pick {max_clips} engaging viral clips of {clip_len} seconds each. "
        f"Return ONLY JSON array of objects with keys start (seconds number), end (seconds number), reason (short string)."
    )

    if ai_engine in ("gemini", "multi") and g_key and g_key.startswith("AIza"):
        try:
            import urllib.request
            models = ["gemini-2.5-flash", "gemini-2.0-flash"]
            for m in models:
                api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={g_key}"
                payload = json.dumps({
                    "contents": [{"parts": [{"text": prompt}]}]
                }).encode("utf-8")
                req = urllib.request.Request(api_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    txt = raw["candidates"][0]["content"]["parts"][0]["text"]
                    match = re.search(r"\[[\s\S]*\]", txt)
                    if match:
                        arr = json.loads(match.group(0))
                        valid = []
                        for item in arr[:max_clips]:
                            st = max(0.0, float(item.get("start", 0)))
                            en = min(float(duration), max(st + 5.0, float(item.get("end", st + clip_len))))
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

    if ai_engine in ("claude", "multi") and c_key:
        try:
            import urllib.request
            payload = json.dumps({
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": c_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                txt = "".join(b.get("text", "") for b in raw.get("content", []))
                match = re.search(r"\[[\s\S]*\]", txt)
                if match:
                    arr = json.loads(match.group(0))
                    valid = []
                    for item in arr[:max_clips]:
                        st = max(0.0, float(item.get("start", 0)))
                        en = min(float(duration), max(st + 5.0, float(item.get("end", st + clip_len))))
                        if en > st:
                            valid.append({
                                "start": st,
                                "end": en,
                                "reason": str(item.get("reason") or "Claude Highlight")[:90],
                            })
                    if valid:
                        return valid
        except Exception:
            pass

    return fallback_moments(duration, max_clips, clip_len)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "clipcutter-api"}), 200


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
    claude_key = (data.get("claude_api_key") or "").strip()
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
            claude_key=claude_key,
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
                        "reason": m.get("reason") or "Best Highlight",
                        "download_url": f"/download/{out_name}",
                    })
                else:
                    results.append({
                        "start": st_hms,
                        "end": en_hms,
                        "error": "Clip file missing",
                    })
            except Exception as clip_err:
                results.append({
                    "start": st_hms,
                    "end": en_hms,
                    "error": str(clip_err),
                })
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
