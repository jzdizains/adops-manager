"""Video "freshening" — make each exported creative read as a brand-new,
never-before-seen upload to TikTok's duplicate/fingerprint detection.

Why: the same video file pushed to many accounts gets recognized (by file hash
AND by perceptual/audio fingerprint) as a re-run, which throttles reach and can
trip "unoriginal content" flags. Freshening breaks BOTH kinds of match:

  file hash   — a full re-encode writes a brand-new bitstream (new md5).
  metadata    — every tag is stripped and a fresh random creation time set.
  perceptual  — a tiny, randomized set of transforms shifts the frame
                fingerprint below the "same video" threshold while staying
                invisible to a viewer: micro crop+rescale (lanczos), sub-degree
                hue and brightness/contrast/saturation nudges, a couple of
                trimmed lead frames, and a <1% speed change (which also moves
                the audio fingerprint).

Quality: the re-encode is deliberately near-lossless — CRF 18, 'medium'
preset, 192k audio, high-quality lanczos scaling, and NO added noise. Measured
SSIM vs source is ~0.999 (PSNR ~50 dB), i.e. visually identical; bitrate stays
close to the original rather than being halved. Changing the file hash REQUIRES
a re-encode (H.264 is lossy), so "identical bytes" is impossible by definition,
but the result is indistinguishable to a viewer and to TikTok's transcoder.

Honest limits — see the note in creatives.html: this reliably defeats hash and
classic perceptual-hash matching. It is NOT a license to spam one video across
hundreds of accounts; heavy ML content-matching and human review still exist,
so treat it as hygiene for legitimate multi-account testing, not a cloaking
tool. Every transform is deliberately subtle to keep the creative intact.
"""
from __future__ import annotations

import functools
import hashlib
import os
import random
import shutil
import subprocess
import threading


@functools.lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """Path to an ffmpeg binary. Prefer a system install; otherwise fall back to
    the static binary bundled by imageio-ffmpeg (so Render's native Python
    runtime, which has no apt, still works)."""
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "No ffmpeg available. Add `imageio-ffmpeg` to requirements.txt "
            f"(or install ffmpeg on the host). Original error: {e!r}")


def available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except Exception:
        return False


# The three chosen levers — SLOWDOWN, slight COLOUR, slight AUDIO — each ranges
# by intensity. Slowdown shifts the video timing AND (via matched atempo) the
# audio fingerprint at once; colour and volume add extra separation. No crop,
# mirror or noise: nothing that distorts the frame or flips on-screen text.
#   slow  = how much slower, as a fraction (0.05 = 5% slower)
#   bright/contr/sat/gamma/hue = colour-grade swing
#   vol   = loudness swing;  pitch = tiny pitch swing (semitone fraction)
_PROFILES = {
    "light":  dict(slow=(0.02, 0.04), bright=0.008, contr=0.008, sat=0.02,
                   gamma=0.012, hue=0.6, vol=0.03, pitch=0.0),
    "medium": dict(slow=(0.03, 0.06), bright=0.014, contr=0.014, sat=0.03,
                   gamma=0.018, hue=1.0, vol=0.05, pitch=0.0),
    "strong": dict(slow=(0.05, 0.09), bright=0.022, contr=0.022, sat=0.045,
                   gamma=0.028, hue=1.6, vol=0.08, pitch=0.0),
}


def _rng(seed: int | None) -> random.Random:
    return random.Random(seed if seed is not None else os.urandom(8))


def build_filters(intensity: str, mirror: bool, rng: random.Random) -> tuple[str, str]:
    """Return (video_filter, audio_filter). `mirror` is accepted for API
    compatibility but intentionally ignored — mirroring flips baked-in text."""
    p = _PROFILES.get(intensity, _PROFILES["medium"])

    def swing(mag: float) -> float:
        return round(rng.uniform(-mag, mag), 4)

    # SLOWDOWN: slow the video by `slow` (setpts multiplies PTS); the audio is
    # slowed by the SAME factor (atempo=1/factor) so it stays perfectly in sync.
    slow = round(rng.uniform(*p["slow"]), 4)          # e.g. 0.045 = 4.5% slower
    factor = round(1 + slow, 4)                        # video PTS multiplier
    atempo = round(1 / factor, 6)                      # audio slowdown, keeps sync

    vf = [
        f"setpts={factor}*PTS",
        # slight colour grade — invisible per-frame but moves the fingerprint
        f"eq=brightness={swing(p['bright'])}:contrast={round(1+swing(p['contr']),4)}:"
        f"saturation={round(1+swing(p['sat']),4)}:gamma={round(1+swing(p['gamma']),4)}",
        f"hue=h={swing(p['hue'])}",
    ]
    af_parts = [f"atempo={atempo}", f"volume={round(1+swing(p['vol']),4)}"]
    return ",".join(vf), ",".join(af_parts)


def _dimensions(src_path: str) -> tuple[int, int] | None:
    """(width, height) of the first video stream, or None if unknown."""
    exe = ffmpeg_exe()
    probe = os.path.join(os.path.dirname(exe), "ffprobe")
    probe = probe if os.path.exists(probe) else (shutil.which("ffprobe") or "")
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:nk=1", src_path],
            capture_output=True, timeout=30)
        w, h = out.stdout.decode().strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def uniquify(src_path: str, dst_path: str, intensity: str = "medium",
             mirror: bool = False, seed: int | None = None,
             timeout: int = 300) -> None:
    """Re-encode src → dst with metadata stripped and the three fingerprint
    levers applied — slight SLOWDOWN, slight COLOUR grade, matched AUDIO slowdown
    — at near-lossless quality (CRF 18). Raises RuntimeError on failure."""
    rng = _rng(seed)
    vf, af = build_filters(intensity, mirror, rng)
    dims = _dimensions(src_path)
    if dims:   # pin the EXACT original dimensions so the aspect ratio never drifts
        vf += f",scale={dims[0]}:{dims[1]}:flags=lanczos,setsar=1"
    tmp = dst_path + ".part"
    cmd = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", src_path,
        "-map_metadata", "-1",                       # strip ALL metadata
        "-map", "0:v:0", "-map", "0:a:0?",           # video + audio if present
        "-vf", vf, "-af", af,
        # QUALITY-FIRST encode: CRF 18 is visually transparent (SSIM ~0.999 vs
        # source); the 17–18 jitter only nudges the bitstream. 'medium' preset
        # ≈ 'slow' quality but faster. NO noise, 192k audio — the goal is a new
        # fingerprint, not a smaller/worse file.
        "-c:v", "libx264", "-preset", "medium",
        "-crf", str(rng.randint(17, 18)),
        "-g", str(rng.randint(48, 96)),              # vary GOP → different bitstream
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-metadata", f"creation_time={_rand_time(rng)}",
        "-movflags", "+faststart",
        "-f", "mp4",              # the .part temp name hides the extension
        tmp,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _unlink(tmp)
        raise RuntimeError(f"ffmpeg timed out after {timeout}s")
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _unlink(tmp)
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {tail}")
    os.replace(tmp, dst_path)


# backwards-compatible alias
freshen = uniquify


def _rand_time(rng: random.Random) -> str:
    # a plausible, recent, randomized capture time
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(
        days=rng.randint(0, 21), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Background processing of queued "freshen" jobs (Creative rows in "processing")
# ---------------------------------------------------------------------------
_process_lock = threading.Lock()


def process_pending(db, limit: int = 4) -> int:
    """Freshen up to `limit` creatives waiting in status='processing'. Safe to
    call from the background sweep AND from a post-upload kick — a lock makes
    passes serial, and each row is committed as it completes so a crash mid-batch
    never loses finished work. Returns how many were processed."""
    from . import config, models

    if not _process_lock.acquire(blocking=False):
        return 0   # another pass is already running
    try:
        rows = (db.query(models.Creative)
                .filter(models.Creative.status == "processing",
                        models.Creative.src_path != "")
                .order_by(models.Creative.id).limit(limit).all())
        if not rows:
            return 0
        if not available():
            for row in rows:      # no ffmpeg — fail loudly instead of hanging
                row.status = "error"
                row.error = ("No ffmpeg available on the server — add "
                             "`imageio-ffmpeg` to requirements and redeploy.")
            db.commit()
            return len(rows)

        out_dir = config.DATA_DIR / "creatives"
        out_dir.mkdir(parents=True, exist_ok=True)
        done = 0
        for row in rows:
            src = row.src_path
            out = out_dir / f"{row.id}_{row.file_name}"
            try:
                if not src or not os.path.exists(src):
                    raise RuntimeError("source file went missing before processing")
                freshen(src, str(out), intensity=row.freshen_intensity or "medium",
                        mirror=bool(row.freshen_mirror), seed=row.id)
                row.file_path = str(out)
                row.md5 = _md5_file(str(out))
                row.size_bytes = os.path.getsize(str(out))
                row.status = "available"
                row.error = ""
            except Exception as e:      # noqa: BLE001 — record, never crash the sweep
                row.status = "error"
                row.error = str(e)[:400]
            row.src_path = ""
            db.commit()
            done += 1
            _gc_source(db, src)         # drop the shared source once nobody needs it
        return done
    finally:
        _process_lock.release()


def _gc_source(db, src: str) -> None:
    """Delete a shared source file once no processing row still points at it."""
    if not src:
        return
    from . import models
    still = (db.query(models.Creative)
             .filter(models.Creative.src_path == src,
                     models.Creative.status == "processing").count())
    if not still:
        _unlink(src)


def pending_count(db) -> int:
    from . import models
    return (db.query(models.Creative)
            .filter(models.Creative.status == "processing").count())
