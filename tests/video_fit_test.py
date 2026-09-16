"""Undersized videos are scaled for TikTok before upload (v120).

Seen live: a small video → upload flagged ILLEGAL_VIDEO_SIZE → /ad/create/ "Unsupported
image size". Now the launcher makes a delivery copy at TikTok's standard frame.

Functional (pure): orientation / too_small / target_for on the documented minimums.
Functional (real ffmpeg, when present): a 360×640 clip → 1080×1920 copy, letterboxed
when the ratio differs, reused on the second call; a 1080×1920 clip is left alone;
a cover frame cut from the copy has the copy's size.
Static: the launch uploads the copy, cuts the cover from the uploaded file, invalidates
a cached upload of the small original, and cleans the copies up with the creative."""
import os, shutil, subprocess, sys, tempfile, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m; return m

pkg = _mod("app"); pkg.__path__ = [os.path.join(ROOT, "app")]
import importlib
vf = importlib.import_module("app.video_fit")

print("\n-- rules --")
check("orientation", vf.orientation(720, 1280) == "vertical" and vf.orientation(1280, 720) == "horizontal" and vf.orientation(640, 640) == "square" and vf.orientation(1080, 1100) == "square")
check("minimums: 540×960 / 640×640 / 960×540 pass", vf.target_for(540, 960) is None and vf.target_for(640, 640) is None and vf.target_for(960, 540) is None and vf.target_for(1080, 1920) is None)
check("one pixel under → the standard frame for that orientation", vf.target_for(539, 960) == (1080, 1920) and vf.target_for(639, 640) == (1080, 1080) and vf.target_for(960, 539) == (1920, 1080))
check("the live case (small vertical) → 1080×1920", vf.target_for(360, 640) == (1080, 1920))
check("unknown size → left alone", vf.target_for(0, 0) is None)

print("\n-- ffmpeg --")
ff = shutil.which("ffmpeg"); fp = shutil.which("ffprobe")
if not (ff and fp):
    print("SKIP: no ffmpeg/ffprobe on this machine")
else:
    freshen = importlib.import_module("app.video_freshen")
    d = tempfile.mkdtemp()
    def clip(name, w, h, secs=2):
        p = os.path.join(d, name)
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(secs), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", p], check=True, capture_output=True)
        return p
    small = clip("small.mp4", 360, 640)
    r = vf.fit(small)
    check("360×640 → a delivery copy exists", r is not None and os.path.exists(r[0]) and r[1] == (360, 640) and r[2] == (1080, 1920))
    dims = freshen._dimensions(r[0]) if r else None
    check("…and it really is 1080×1920", dims == (1080, 1920), str(dims))
    m1 = os.path.getmtime(r[0]); r2 = vf.fit(small)
    check("second call reuses the copy (no re-encode)", r2 is not None and r2[0] == r[0] and os.path.getmtime(r[0]) == m1)
    odd = clip("odd.mp4", 300, 400)          # 3:4 — vertical but not 9:16 → letterboxed into 1080×1920
    r3 = vf.fit(odd)
    check("an off-ratio clip is letterboxed into the standard frame, not stretched", r3 is not None and freshen._dimensions(r3[0]) == (1080, 1920))
    big = clip("big.mp4", 1080, 1920, 1)
    check("a 1080×1920 clip is left alone", vf.fit(big) is None and not os.path.exists(vf.delivery_path(big)))
    # cover frame from the delivery copy has the copy's size
    _mod("sqlalchemy.orm", Session=object)
    ca = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
    src = ca[ca.index("def _cover_frame(file_path: str) -> str:"):]
    src = src[:src.index("\n\n\n")]
    ns = {}
    exec("from app import video_freshen\n" + src.replace("from .. import video_freshen", "pass"), ns)
    cover = ns["_cover_frame"](r[0])
    out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:nk=1", cover], capture_output=True).stdout.decode().strip()
    check("cover frame cut from the copy is 1080×1920", out.startswith("1080,1920"), out)
    shutil.rmtree(d, ignore_errors=True)

print("\n-- static --")
ca = open(os.path.join(ROOT, "app", "routes", "campaigns.py"), encoding="utf-8").read()
check("the launch fits the file before uploading and uploads the copy", "scaled = video_fit.fit(path)" in ca and "tiktok_api.upload_video_file(acct.access_token, acct.advertiser_id, path," in ca)
check("the cover is resolved from the uploaded file (same size as the video)", "_resolve_cover(acct, video_id, poster, creative.id, path)" in ca and "tiktok_api.upload_image_file(acct.access_token, acct.advertiser_id, frame" in ca)
check("a cached upload of the small original is dropped so the copy goes up on retry", 'if (cached.upload_md5 or "") != video_freshen._md5_file(path):' in ca and "db.delete(cached)" in ca)
check("the delivered file's md5 is recorded", 'upload_md5=_vf._md5_file(path) if scaled else ""' in ca)
check("delivery copies are deleted with the creative", '".ttfit.mp4", ".cover.jpg"' in open(os.path.join(ROOT, "app", "routes", "creatives.py"), encoding="utf-8").read())

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
