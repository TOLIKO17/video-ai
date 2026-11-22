import os, uuid, shutil, subprocess, threading
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/video-ai/uploads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/video-ai/outputs"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video AI API", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"]
)
jobs = {}

class JobOptions(BaseModel):
    upscale: bool = True
    denoise: bool = True
    fps60: bool = False
    hdr: bool = False

def run_job(job_id: str, source: Path, options: JobOptions):
    output = OUTPUT_DIR / f"{job_id}.mp4"
    jobs[job_id]["status"] = "processing"
    try:
        filters = []
        if options.denoise:
            # FFmpeg HQDN3D spatial/temporal denoising.
            filters.append("hqdn3d=1.5:1.5:6:6")
        if options.upscale:
            # Fit within 1920x1080 without distorting aspect ratio.
            filters.append("scale='min(1920,iw*min(1920/iw,1080/ih))':'min(1080,ih*min(1920/iw,1080/ih))':flags=lanczos")
        if options.hdr:
            # Conservative SDR tone/contrast enhancement, not true HDR reconstruction.
            filters.append("eq=contrast=1.08:brightness=0.02:saturation=1.08")
        if options.fps60:
            filters.append("minterpolate=fps=60:mi_mode=mci:mc_mode=obmc:me_mode=bidir")
        cmd = ["ffmpeg", "-y", "-i", str(source)]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60*60*6)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-2500:])
        jobs[job_id].update(status="completed", download_url=f"/api/jobs/{job_id}/download",
                            filename=output.name)
    except Exception as exc:
        jobs[job_id].update(status="failed", error=str(exc)[:3000])
    finally:
        try: source.unlink(missing_ok=True)
        except Exception: pass

@app.get("/api/health")
def health():
    return {"ok": True, "service": "Video AI API"}

@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    upscale: bool = Form(True),
    denoise: bool = Form(True),
    fps60: bool = Form(False),
    hdr: bool = Form(False)
):
    if not file.filename or Path(file.filename).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        raise HTTPException(400, "Поддерживаются MP4, MOV, MKV, WEBM и AVI.")
    job_id = uuid.uuid4().hex
    source = UPLOAD_DIR / f"{job_id}{Path(file.filename).suffix.lower()}"
    size = 0
    try:
        with source.open("wb") as dest:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"Максимальный размер файла: {MAX_UPLOAD_MB} МБ.")
                dest.write(chunk)
    except Exception:
        source.unlink(missing_ok=True)
        raise
    jobs[job_id] = {"status": "queued", "filename": file.filename}
    options = JobOptions(upscale=upscale, denoise=denoise, fps60=fps60, hdr=hdr)
    background_tasks.add_task(run_job, job_id, source, options)
    return {"job_id": job_id, "status": "queued"}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена. Перезапустите загрузку.")
    return {"job_id": job_id, **job}

@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(404, "Готовое видео пока недоступно.")
    path = OUTPUT_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "Файл уже недоступен.")
    return FileResponse(path, media_type="video/mp4", filename="video-ai-enhanced.mp4")
