import os
import uuid
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ClipRequest(BaseModel):
    url: str
    start: str
    end: str

def cleanup_file(path: str):
    if os.path.exists(path):
        os.remove(path)

@app.get("/")
def health_check():
    return {"status": "online", "message": "ClipCutter API is running"}

@app.post("/clip")
def create_clip(req: ClipRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    output_file = f"/tmp/clip_{job_id}.mp4"

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--download-sections", f"*{req.start}-{req.end}",
        "--force-keyframes-at-cuts",
        "-o", output_file,
        req.url
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {e.stderr.decode('utf-8', errors='ignore')}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not os.path.exists(output_file):
        raise HTTPException(status_code=500, detail="Clip file could not be generated.")

    background_tasks.add_task(cleanup_file, output_file)
    return FileResponse(output_file, media_type="video/mp4", filename=f"short_{job_id}.mp4")
