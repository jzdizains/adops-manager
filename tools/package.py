"""Package the dashboard for delivery: writes app/build_manifest.json (the code files this
zip ships, in fingerprint order) and BUILD.txt (the fingerprint the running server will show
on Settings › Server once this zip is deployed), then zips the tree without data, caches or
secrets. Usage: python tools/package.py [/path/to/out.zip]"""
import json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

mf = APP / config.MANIFEST
if mf.exists():
    mf.unlink()
files = [str(p.relative_to(APP)).replace("\\", "/") for p in config.code_files(APP)]
mf.write_text(json.dumps({"files": files}, indent=0) + "\n", encoding="utf-8")
config._BUILD_ID = ""
bid = config.build_id()
(ROOT / "BUILD.txt").write_text(bid + "\n", encoding="utf-8")
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "adops-manager.zip").resolve()
if out.exists():
    out.unlink()
subprocess.run(["zip", "-qr", str(out), ROOT.name, "-x", "*/__pycache__/*", "*.pyc", "*/.pytest_cache/*",
                f"{ROOT.name}/data/*", "*.db", "*.ttfit.mp4", "*/.env", "*/.env.*", "*/cookie.txt", "*/done.csv", "*/accounts.txt"], cwd=ROOT.parent, check=True)
print(f"{out}  build {bid}  {len(files)} code files")
