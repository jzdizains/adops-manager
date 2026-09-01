"""TensorPix API client — AI video enhancement, used to turn one uploaded
creative into N distinct high-quality variations (each a genuinely new render,
so it reads as a brand-new video to TikTok's fingerprinting).

API shape (docs.tensorpix.ai):
  Auth:     Authorization: Token <TENSORPIX_API_KEY>
  Upload:   POST /api/videos/            (multipart, field "file") -> {id}
            then poll GET /api/videos/{id}/ until "file" (URL) is non-null
  Models:   GET  /api/ml-models/         -> [{id,name,task,upscale_factor,...}]
  Job:      POST /api/jobs/              {input_video, ml_models:[id], codec,
                                          container, crf, output_resolution, ...}
                                         -> {id, cost_usd, ...}
            poll GET /api/jobs/{id}/     status 0 queue,1 processing,2 done,
                                          -1 failed,-2 cancelled; output_video.file
  Download: GET the pre-signed output_video.file URL.

Each variation submits the SAME model on the SAME uploaded source but with a
per-variant nudge (crf jitter + a few trimmed lead frames + optional sharpen/
grain), so the outputs differ from each other and from the source while staying
high quality. All calls raise TensorPixError with a readable message on failure;
the caller records it on the creative row instead of crashing.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import config

TIMEOUT = httpx.Timeout(60.0, connect=15.0)
UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=15.0)   # big files


class TensorPixError(Exception):
    def __init__(self, message: str, status: int | None = None, data: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.data = data


def configured() -> bool:
    return bool(config.TENSORPIX_API_KEY)


def _headers() -> dict:
    if not config.TENSORPIX_API_KEY:
        raise TensorPixError("TensorPix API key not set — add TENSORPIX_API_KEY "
                             "in the server environment.")
    return {"Authorization": f"Token {config.TENSORPIX_API_KEY}"}


def _url(path: str) -> str:
    return f"{config.TENSORPIX_BASE.rstrip('/')}{path}"


def _parse(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            j = resp.json()
            detail = j.get("detail") or j.get("message") or str(j)[:400]
        except Exception:
            pass
        raise TensorPixError(f"TensorPix HTTP {resp.status_code}: {detail}",
                             status=resp.status_code)
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        raise TensorPixError(f"TensorPix returned non-JSON (HTTP {resp.status_code})")


def _get(path: str) -> Any:
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            return _parse(c.get(_url(path), headers=_headers()))
    except httpx.HTTPError as e:
        raise TensorPixError(f"Network error calling TensorPix: {e!r}")


def _post(path: str, json: dict) -> Any:
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            return _parse(c.post(_url(path), headers=_headers(), json=json))
    except httpx.HTTPError as e:
        raise TensorPixError(f"Network error calling TensorPix: {e!r}")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
# task-group ids (from the docs) → label, for a friendlier picker
TASKS = {
    1: "Super Resolution", 2: "Film damage", 3: "Denoise", 4: "Sharpen",
    5: "VHS Prettify", 6: "Dropouts Buster", 7: "Auto Color Balance",
    8: "Decompressor", 9: "Deinterlace", 10: "FPS Interpolation",
    11: "Slow Motion", 12: "Face Enhance", 13: "Stabilization",
    14: "Audio Denoise", 15: "Low Light Enhance",
}


def list_models() -> list[dict]:
    """All available AI filters. Each: {id, name, task, upscale_factor, ...}."""
    data = _get("/api/ml-models/")
    if isinstance(data, dict):
        data = data.get("results", data.get("list", []))
    return data or []


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def upload_video(file_path: str, file_name: str | None = None) -> str:
    """Upload a local file, return the TensorPix video id (as str). The upload
    is async server-side — call video_ready() before creating a job."""
    name = file_name or os.path.basename(file_path)
    try:
        with open(file_path, "rb") as fh, httpx.Client(timeout=UPLOAD_TIMEOUT) as c:
            resp = c.post(_url("/api/videos/"), headers=_headers(),
                          files={"file": (name, fh, "video/mp4")})
        data = _parse(resp)
    except httpx.HTTPError as e:
        raise TensorPixError(f"Network error uploading to TensorPix: {e!r}")
    except OSError as e:
        raise TensorPixError(f"Could not read source file: {e!r}")
    vid = data.get("id")
    if not vid:
        raise TensorPixError(f"Upload response missing id: {str(data)[:200]}")
    return str(vid)


def video_ready(video_id: str) -> bool:
    """True once cloud storage has the file (the `file` URL is populated)."""
    data = _get(f"/api/videos/{video_id}/")
    return bool(data.get("file"))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def create_job(video_id: str, model_ids: list[int], *, crf: int = 18,
               output_resolution: int = -1, start_frame: int = 0,
               sharpen_strength: float | None = None,
               grain: float | None = None, codec: str = "libx264",
               container: str = "mp4") -> dict:
    """Create an enhancement job. output_resolution=-1 keeps the source size.
    Returns the job dict (id, cost_usd, ...)."""
    payload: dict = {
        "input_video": int(video_id),
        "ml_models": [int(m) for m in model_ids],
        "codec": codec, "container": container,
        "crf": int(crf), "output_resolution": int(output_resolution),
    }
    if start_frame:
        payload["start_frame"] = int(start_frame)
    if sharpen_strength is not None:
        payload["sharpen_strength"] = float(sharpen_strength)
    if grain is not None:
        payload["grain"] = float(grain)
    return _post("/api/jobs/", payload)


# job status codes
QUEUED, PROCESSING, FINISHED, FAILED, CANCELLED = 0, 1, 2, -1, -2


def get_job(job_id: str) -> dict:
    return _get(f"/api/jobs/{job_id}/")


def job_output_url(job: dict) -> str:
    ov = job.get("output_video") or {}
    return ov.get("file") or ""


def download(url: str, dst_path: str, timeout: int = 600) -> None:
    """Stream a finished output video to disk (constant memory)."""
    tmp = dst_path + ".part"
    try:
        with httpx.Client(timeout=httpx.Timeout(float(timeout), connect=15.0),
                          follow_redirects=True) as c:
            with c.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise TensorPixError(f"Download failed HTTP {resp.status_code}")
                with open(tmp, "wb") as out:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        out.write(chunk)
    except httpx.HTTPError as e:
        _rm(tmp)
        raise TensorPixError(f"Network error downloading result: {e!r}")
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _rm(tmp)
        raise TensorPixError("Downloaded output was empty")
    os.replace(tmp, dst_path)


def _rm(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass
